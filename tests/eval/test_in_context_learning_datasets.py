# Copyright 2024 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

import contextlib
import os
import random
from pathlib import Path
from typing import Optional

import pytest
import torch
import transformers
from composer import Evaluator
from composer.core import DataSpec
from composer.loggers import InMemoryLogger
from composer.models import HuggingFaceModel
from composer.trainer import Trainer
from composer.utils import dist, reproducibility
from torch.utils.data import DataLoader
from transformers import PreTrainedTokenizerBase

from llmfoundry.eval.datasets import (
    InContextLearningDataset,
    InContextLearningGenerationTaskWithAnswersDataset,
    InContextLearningMultipleChoiceTaskDataset,
    InContextLearningSchemaTaskDataset,
    MultiTokenEOSCriteria,
    get_continuation_span,
    get_fewshot_sample_idxs,
    get_icl_task_dataloader,
    make_padded_input,
    strip_data,
    tokenizer_needs_prefix_space,
    trim_context,
)
from llmfoundry.eval.metrics import (
    InContextLearningGenerationExactMatchAccuracy,
    InContextLearningLMAccuracy,
    InContextLearningMultipleChoiceAccuracy,
)
from llmfoundry.utils.builders import build_evaluators, build_icl_evaluators


def test_strip_data():
    data_to_strip = {
        'strip_data': '  boo!  \n',
        'has_space': '  wa hoo!',
        'end_space': 'yoohoo!  ',
    }
    stripped_data = strip_data(data_to_strip)
    for k, v in stripped_data.items():
        assert k in data_to_strip
        assert not v[0].isspace()
        assert not v[-1].isspace()


def test_tokenizer_needs_prefix_space_when_space_not_needed(
    tiny_llama_tokenizer: transformers.PreTrainedTokenizerBase,
):
    assert not tokenizer_needs_prefix_space(tiny_llama_tokenizer)


def test_tokenizer_needs_prefix_space_when_space_needed(
    tiny_gpt2_tokenizer: transformers.PreTrainedTokenizerBase,
):
    assert tokenizer_needs_prefix_space(tiny_gpt2_tokenizer)


def test_trim_context():
    context = [0] * 99 + [1] * 2037
    continuation = [2] * 10
    max_seq_len = 2048
    trimmed_context = trim_context(
        context,
        continuation,
        max_seq_len=max_seq_len,
    )
    assert len(trimmed_context) == 2038
    assert trimmed_context[0] == 0
    assert trimmed_context[1] == 1


def test_trim_context_no_continuation():
    context = [0] * 2048
    max_seq_len = 2048
    trimmed_context = trim_context(context, [], max_seq_len=max_seq_len)
    assert len(trimmed_context) == 2048
    context = [0] * 3000 + [1]
    max_seq_len = 2048
    trimmed_context = trim_context(context, [], max_seq_len=max_seq_len)
    assert len(trimmed_context) == 2048
    assert trimmed_context[-1] == 1


def test_get_continuation_span():
    context = [0] * 200
    continuation = [1] * 3
    cont_span = get_continuation_span(context, continuation)
    assert torch.all(torch.eq(cont_span, torch.tensor([200, 201, 202])))
    continuation = [1]
    cont_span = get_continuation_span(context, continuation)
    assert torch.all(torch.eq(cont_span, torch.tensor([200])))


@pytest.mark.parametrize('padding_side', ['left', 'right', 'middle'])
def test_make_padding(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    padding_side: str,
):
    context = tiny_gpt2_with_pad_tokenizer(' cat' * 2000)['input_ids']
    padding_id = tiny_gpt2_with_pad_tokenizer.eos_token_id

    error_context = contextlib.nullcontext() if padding_side in {
        'left',
        'right',
    } else pytest.raises(ValueError)

    with error_context:
        input_ids = make_padded_input(
            context,
            [],
            2048,
            padding_id,
            padding_side=padding_side,
        )

        if padding_side == 'left':
            assert input_ids[0] == tiny_gpt2_with_pad_tokenizer.eos_token_id
            assert input_ids[48:].tolist() == context
        elif padding_side == 'right':
            assert input_ids[-1] == tiny_gpt2_with_pad_tokenizer.eos_token_id
            assert input_ids[:-48].tolist() == context


def test_batch_padding_logic_no_padding(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
):
    continuation = tiny_gpt2_with_pad_tokenizer(' dog' * 2000)['input_ids']
    context = tiny_gpt2_with_pad_tokenizer(' cat' * 2000)['input_ids']
    max_seq_len = 2048
    trimmed_context = trim_context(context, continuation, max_seq_len)
    continuation_spans = get_continuation_span(trimmed_context, continuation)
    padded_input = make_padded_input(
        trimmed_context,
        continuation,
        max_seq_len,
        tiny_gpt2_with_pad_tokenizer.pad_token_id,
        padding_side='right',
    )
    assert continuation_spans[0] == 48 and continuation_spans[-1] == 2047
    assert len(padded_input) == 2048
    assert tiny_gpt2_with_pad_tokenizer.pad_token_id not in padded_input


def test_batch_padding_logic_with_padding(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
):
    continuation = tiny_gpt2_with_pad_tokenizer(' dog' * 200)['input_ids']
    context = tiny_gpt2_with_pad_tokenizer(' cat' * 200)['input_ids']
    max_seq_len = 2048
    trimmed_context = trim_context(context, continuation, max_seq_len)
    continuation_spans = get_continuation_span(trimmed_context, continuation)
    padded_input = make_padded_input(
        trimmed_context,
        continuation,
        max_seq_len,
        tiny_gpt2_with_pad_tokenizer.pad_token_id,
        padding_side='right',
    )
    assert continuation_spans[0] == 200 and continuation_spans[-1] == 399
    assert len(padded_input) == 2048
    assert padded_input[-1] == tiny_gpt2_with_pad_tokenizer.pad_token_id


def test_fewshot_sample_idxs():
    rng = random.Random(1234)

    fewshot_idxs = get_fewshot_sample_idxs(
        dataset_size=5,
        num_fewshot=4,
        example_idx=4,
        rng=rng,
    )
    assert fewshot_idxs == {0, 1, 2, 3}

    fewshot_idxs = get_fewshot_sample_idxs(
        dataset_size=5,
        num_fewshot=5,
        example_idx=4,
        rng=rng,
    )
    assert fewshot_idxs == {0, 1, 2, 3}

    fewshot_idxs = get_fewshot_sample_idxs(
        dataset_size=5,
        num_fewshot=500,
        example_idx=4,
        rng=rng,
    )
    assert fewshot_idxs == {0, 1, 2, 3}

    fewshot_idxs = get_fewshot_sample_idxs(
        dataset_size=10,
        num_fewshot=7,
        example_idx=4,
        rng=rng,
    )
    assert len(fewshot_idxs) == 7 and 4 not in fewshot_idxs


def test_fewshot_sample_idxs_randomness():
    dataset_size = 10000
    num_fewshot = 5

    rng_1_seed_1234 = random.Random(1234)
    rng_2_seed_1234 = random.Random(1234)
    rng_3_seed_11 = random.Random(11)

    rng_1_sample_1 = get_fewshot_sample_idxs(
        dataset_size,
        num_fewshot,
        1,
        rng_1_seed_1234,
    )
    rng_2_sample_1 = get_fewshot_sample_idxs(
        dataset_size,
        num_fewshot,
        1,
        rng_2_seed_1234,
    )
    rng_3_sample_1 = get_fewshot_sample_idxs(
        dataset_size,
        num_fewshot,
        1,
        rng_3_seed_11,
    )

    assert rng_1_sample_1 == rng_2_sample_1
    assert rng_1_sample_1 != rng_3_sample_1

    rng_1_sample_2 = get_fewshot_sample_idxs(
        dataset_size,
        num_fewshot,
        2,
        rng_1_seed_1234,
    )
    rng_2_sample_2 = get_fewshot_sample_idxs(
        dataset_size,
        num_fewshot,
        2,
        rng_2_seed_1234,
    )
    rng_3_sample_2 = get_fewshot_sample_idxs(
        dataset_size,
        num_fewshot,
        2,
        rng_3_seed_11,
    )

    assert rng_1_sample_2 == rng_2_sample_2
    assert rng_1_sample_2 != rng_3_sample_2


@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_update_generation_kwargs(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    hf_loading_vars = {
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }
    hf_parsing_map = {'context': ['quas', 'wex', 'exort'], 'answer': ['spell']}
    gen_kwargs = {'test_arg1': 1, 'test_arg2': 2}

    dl = InContextLearningDataset(
        dataset_uri='hf://mosaicml/test_dataset',
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        prelimiter='Orbs: ',
        continuation_delimiter='\nSpell:',
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
        generation_kwargs=gen_kwargs,
    )
    assert dl.base_batch['generation_kwargs'] == {
        'test_arg1': 1,
        'test_arg2': 2,
    }


def test_stop_sequences_criteria(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
):
    eos_criteria = MultiTokenEOSCriteria(
        '\n\n',
        tiny_gpt2_with_pad_tokenizer,
        2,
    )
    seq1 = tiny_gpt2_with_pad_tokenizer('Dogs are furry')['input_ids']
    seq2 = tiny_gpt2_with_pad_tokenizer('Dogs are furry\n\n')['input_ids']
    seq1 = [
        tiny_gpt2_with_pad_tokenizer.pad_token_id,
    ] * (len(seq2) - len(seq1)) + seq1
    input_ids = torch.LongTensor([seq1, seq2])
    assert not eos_criteria(
        input_ids,
        None,
    )  # pyright: ignore[reportGeneralTypeIssues]

    eos_criteria = MultiTokenEOSCriteria(
        '\n\n',
        tiny_gpt2_with_pad_tokenizer,
        2,
    )
    seq1 = tiny_gpt2_with_pad_tokenizer('Dogs are furry\n\n')['input_ids']
    seq2 = tiny_gpt2_with_pad_tokenizer('Dogs are furry\n\n')['input_ids']
    input_ids = torch.LongTensor([seq1, seq2])
    assert eos_criteria(
        input_ids,
        None,
    )  # pyright: ignore[reportGeneralTypeIssues]


def test_stop_sequences_criteria_sentencepiece(
    tiny_llama_tokenizer: transformers.AutoTokenizer,
):

    tokenizer = tiny_llama_tokenizer
    eos_criteria = MultiTokenEOSCriteria('\n\n', tokenizer, 2)
    seq1 = tokenizer(
        '\n\nDogs',
    )['input_ids'
     ]  # check to make sure starting with the stop sequence doesnt break it
    seq2 = tokenizer('Dogs are furry\n\n')['input_ids']
    seq1 = [tokenizer.eos_token_id] * (len(seq2) - len(seq1)) + seq1
    input_ids = torch.LongTensor([seq1, seq2])
    assert not eos_criteria(
        input_ids,
        None,
    )  # pyright: ignore[reportGeneralTypeIssues]

    eos_criteria = MultiTokenEOSCriteria('\n\n', tokenizer, 2)
    seq1 = tokenizer('Dogs are furry\n\n')['input_ids']
    seq2 = tokenizer('Dogs are furry\n\n')['input_ids']
    input_ids = torch.LongTensor([seq1, seq2])
    assert eos_criteria(
        input_ids,
        None,
    )  # pyright: ignore[reportGeneralTypeIssues]


@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_update_generation_kwargs_no_kwargs(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    hf_loading_vars = {
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }
    hf_parsing_map = {'context': ['quas', 'wex', 'exort'], 'answer': ['spell']}

    dl = InContextLearningDataset(
        dataset_uri='hf://mosaicml/test_dataset',
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        prelimiter='Orbs: ',
        continuation_delimiter='\nSpell:',
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
    )
    assert not 'generation_kwargs' in dl.base_batch


def test_update_generation_kwargs_no_kwargs_qa_dataset(
    tmp_path: Path,
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/triviaqa_small.jsonl'

    tokenizer = tiny_gpt2_tokenizer

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=1024,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='\n',
        continuation_delimiter=': ',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
        generation_kwargs=None,
    )
    assert len(dl.base_batch['generation_kwargs']) == 4


def test_update_generation_kwargs_with_kwargs_qa_dataset(
    tmp_path: Path,
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/triviaqa_small.jsonl'

    tokenizer = tiny_gpt2_tokenizer

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=1024,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='\n',
        continuation_delimiter=': ',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
        generation_kwargs={'temperature': 0.9},
    )
    assert 'generation_kwargs' in dl.base_batch
    assert dl.base_batch['generation_kwargs']['temperature'] == 0.9
    assert len(dl.base_batch['generation_kwargs']) == 5


@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_construct_context(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    hf_loading_vars = {
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }
    hf_parsing_map = {'context': ['quas', 'wex', 'exort'], 'answer': ['spell']}

    dl = InContextLearningDataset(
        dataset_uri='hf://mosaicml/test_dataset',
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        prelimiter='Orbs: ',
        continuation_delimiter='\nSpell: ',
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
    )
    constructed_context = dl.construct_context({
        'context': 'quas quas exort',
        'answer': 'ice wall',
    })
    assert constructed_context == 'Orbs: quas quas exort\nSpell: '
    constructed_context = dl.construct_context({
        'context': 'quas quas exort',
        'answer': 'ice wall',
    },
                                               add_answer=True)
    assert constructed_context == 'Orbs: quas quas exort\nSpell: ice wall'
    constructed_context = dl.construct_context(
        {
            'context': 'quas quas exort',
            'answer': 'ice wall',
        },
        preceding_text='The harsh White Waste beckons!',
        add_answer=True,
    )
    assert constructed_context == '\nOrbs: quas quas exort\nSpell: ice wall'


@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_get_answer_from_example(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    hf_loading_vars = {
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }
    hf_parsing_map = {'context': ['quas', 'wex', 'exort'], 'answer': ['spell']}

    dl = InContextLearningDataset(
        dataset_uri='hf://mosaicml/test_dataset',
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        prelimiter='Orbs: ',
        continuation_delimiter='\nSpell:',
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
    )
    answer = dl.get_answer_from_example({
        'context': 'wex exort exort',
        'answer': 'alacrity',
    })
    assert answer == ' alacrity'


@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_fix_eos_on_preamble(
    tmp_path: Path,
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
):
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    hf_loading_vars = {
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }
    hf_parsing_map = {'context': ['quas', 'wex', 'exort'], 'answer': ['spell']}
    tiny_gpt2_tokenizer.eos_token_id = 50256

    dl = InContextLearningDataset(
        dataset_uri='hf://mosaicml/test_dataset',
        tokenizer=tiny_gpt2_tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tiny_gpt2_tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        prelimiter='Orbs: ',
        continuation_delimiter='\nSpell:',
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
    )
    preamble = 'blah blah blah.'
    tokenized_preamble = tiny_gpt2_tokenizer.encode(preamble)
    tokenized_preamble += [tiny_gpt2_tokenizer.eos_token_id]
    fixed_preamble = dl._fix_eos_on_preamble(tokenized_preamble)
    assert tokenized_preamble[:-1] == fixed_preamble
    assert fixed_preamble[-1] != tiny_gpt2_tokenizer.eos_token_id


@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_tokenize_example_with_tokenize_labels(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    hf_loading_vars = {
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }
    hf_parsing_map = {'context': ['quas', 'wex', 'exort'], 'answer': ['spell']}

    dl = InContextLearningDataset(
        dataset_uri='hf://mosaicml/test_dataset',
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        prelimiter='Orbs: ',
        continuation_delimiter='\nSpell: ',
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
        tokenize_labels=True,
    )
    tokenized_example = dl.tokenize_example(
        'What spell does this invoke? ',
        'exort exort wex\nSpell: ',
        {'answer': ' Meatball'},
    )
    tokenized_input = [
        2061,
        4822,
        857,
        428,
        26342,
        30,
        220,
        1069,
        419,
        409,
        419,
        356,
        87,
        198,
        31221,
        25,
        19145,
        1894,
    ]
    assert tokenized_example['context'][:len(tokenized_input)].tolist(
    ) == tokenized_input
    assert tokenized_example['context'][-1] == tokenizer.eos_token_id
    assert type(tokenized_example['answer'][0]) == int
    assert len(tokenized_example['context']) == seqlen
    assert 'continuation_indices' in tokenized_example


@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_tokenize_example_with_no_tokenize_labels(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    hf_loading_vars = {
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }
    hf_parsing_map = {'context': ['quas', 'wex', 'exort'], 'answer': ['spell']}

    dl = InContextLearningDataset(
        dataset_uri='hf://mosaicml/test_dataset',
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        prelimiter='Orbs: ',
        continuation_delimiter='\nSpell: ',
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
        tokenize_labels=False,
    )
    tokenized_example = dl.tokenize_example(
        'What spell does this invoke? ',
        'exort exort wex\nSpell: ',
        {'answer': ' Meatball'},
    )
    tokenized_input = [
        2061,
        4822,
        857,
        428,
        26342,
        30,
        220,
        1069,
        419,
        409,
        419,
        356,
        87,
        198,
        31221,
        25,
    ]
    assert tokenized_example['context'][:len(tokenized_input)].tolist(
    ) == tokenized_input
    assert tokenized_example['context'][-1] == tokenizer.eos_token_id
    assert len(tokenized_example['context']) == seqlen
    assert type(tokenized_example['answer']) == str


def test_qa_set_cot_no_cot(
    tmp_path: Path,
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/triviaqa_small.jsonl'

    tokenizer = tiny_gpt2_tokenizer

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=1024,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='\n',
        continuation_delimiter=': ',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
    )
    assert not dl.has_cot


def test_qa_set_cot_has_cot(
    tmp_path: Path,
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/gsm8k_small.jsonl'

    tokenizer = tiny_gpt2_tokenizer

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=1024,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='\n',
        continuation_delimiter=': ',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
    )
    assert dl.has_cot


def test_qa_get_max_answer_length(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/triviaqa_small.jsonl'
    tokenizer = tiny_gpt2_with_pad_tokenizer

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=1024,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='',
        continuation_delimiter='',
        cot_delimiter='',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
    )
    # empirical number from the small test dataset
    assert dl.max_answer_length == 7


def test_qa_get_answer_from_example_with_no_cot(
    tmp_path: Path,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/triviaqa_small.jsonl'

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tiny_gpt2_with_pad_tokenizer,
        max_seq_len=1024,
        pad_tok_id=tiny_gpt2_with_pad_tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='\n',
        continuation_delimiter=': ',
        cot_delimiter=' ### ',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
    )
    answer = dl.get_answer_from_example({
        'context': 'empty',
        'answer': 'this is the correct answer',
        'chain_of_thought': "Let's think step by step. ",
    })
    assert answer == 'this is the correct answer'


def test_qa_get_answer_from_example_with_cot(
    tmp_path: Path,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/triviaqa_small.jsonl'

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tiny_gpt2_with_pad_tokenizer,
        max_seq_len=1024,
        pad_tok_id=tiny_gpt2_with_pad_tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='\n',
        continuation_delimiter=': ',
        cot_delimiter=' ### ',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
    )
    dl.has_cot = True
    answer = dl.get_answer_from_example({
        'context': 'empty',
        'answer': 'this is the correct answer',
        'chain_of_thought': "Let's think step by step. ",
    })
    assert answer == "Let's think step by step.  ### this is the correct answer"


def test_qa_tokenize_example(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/triviaqa_small.jsonl'

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = InContextLearningGenerationTaskWithAnswersDataset(
        dataset_uri=dataset_uri,
        tokenizer=tiny_gpt2_with_pad_tokenizer,
        max_seq_len=1024,
        pad_tok_id=tiny_gpt2_with_pad_tokenizer.eos_token_id,
        num_fewshot=0,
        fewshot_random_seed=1234,
        prompt_string='',
        example_delimiter='\n',
        continuation_delimiter=': ',
        cot_delimiter=' ### ',
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
    )
    dl.has_cot = True
    tokenized_example = dl.tokenize_example(
        'starting prompt',
        'a context',
        {
            'context': 'empty',
            'answer': 'this is the correct answer',
            'aliases': ['this is the right answer', 'this is the best answer'],
            'chain_of_thought': "Let's think step by step. ",
        },
    )
    assert 'aliases' in tokenized_example
    assert tokenized_example['aliases'] == [
        'this is the right answer',
        'this is the best answer',
    ]


def test_mc_tokenize_example(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/mmlu_small.jsonl'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    seqlen = 2048
    dl = InContextLearningMultipleChoiceTaskDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,
        example_delimiter='\n',
        continuation_delimiter=' ### ',
        destination_path=str(tmp_path / 'test_human_eval_small.jsonl'),
    )
    example = {
        'context':
            "Who's the best eval researcher?\n A. Jeremy\n B. Tessa\n C. Max\n D. Other\nAnswer: ",
        'choices': ['A', 'B', 'C', 'D'],
        'gold':
            2,
    }
    tokenized_example = dl.tokenize_example(
        prompt_and_fewshot='Answer the following: ',
        ctxt=example['context'],
        example=example,
    )
    unpadded_queries = [
        context[context != tokenizer.eos_token_id]
        for context in tokenized_example['query']
    ]
    untokenized_inputs = [
        tokenizer.decode(unpadded_input) for unpadded_input in unpadded_queries
    ]
    correct_output = [
        "Answer the following: Who's the best eval researcher?\n A. Jeremy\n B. Tessa\n C. Max\n D. Other\nAnswer: A",
        "Answer the following: Who's the best eval researcher?\n A. Jeremy\n B. Tessa\n C. Max\n D. Other\nAnswer: B",
        "Answer the following: Who's the best eval researcher?\n A. Jeremy\n B. Tessa\n C. Max\n D. Other\nAnswer: C",
        "Answer the following: Who's the best eval researcher?\n A. Jeremy\n B. Tessa\n C. Max\n D. Other\nAnswer: D",
    ]
    assert untokenized_inputs == correct_output


@pytest.mark.parametrize('prelimiter', ['', 'This is a question: '])
def test_schema_construct_context(
    prelimiter: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/winograd_small.jsonl'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    seqlen = 2048
    dl = InContextLearningSchemaTaskDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string='',
        prelimiter=prelimiter,
        example_delimiter='\n',
        continuation_delimiter=' ### ',
        destination_path=str(tmp_path / 'test_human_eval_small.jsonl'),
    )
    example = {
        'context_options': ['cont one', 'cont two'],
        'gold': 0,
        'continuation': 'this is a continuation',
    }
    constructed_context = dl.construct_context(example)
    assert constructed_context == f'{prelimiter}cont one ### this is a continuation'
    constructed_context = dl.construct_context(example, preceding_text='text')
    assert constructed_context == f'{prelimiter}\ncont one ### this is a continuation'


@pytest.mark.parametrize('prelimiter', ['', 'This is a question: '])
def test_schema_construct_multiple_contexts(
    prelimiter: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/winograd_small.jsonl'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    seqlen = 2048
    dl = InContextLearningSchemaTaskDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prelimiter=prelimiter,
        prompt_string=prompt_string,
        example_delimiter='\n',
        continuation_delimiter=' ### ',
        destination_path=str(tmp_path / 'test_human_eval_small.jsonl'),
    )
    example = {
        'context_options': [f'cont one', 'cont two'],
        'gold': 0,
        'continuation': 'this is a continuation',
    }
    constructed_contexts = dl._construct_multiple_contexts(example)
    assert constructed_contexts == [
        f'{prelimiter}cont one',
        f'{prelimiter}cont two',
    ]
    constructed_contexts = dl._construct_multiple_contexts(
        example,
        preceding_text='some text',
    )
    assert constructed_contexts == [
        f'{prelimiter}\ncont one ###',
        f'{prelimiter}\ncont two ###',
    ]


def test_schema_tokenize_example(
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/winograd_small.jsonl'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    seqlen = 2048
    num_fewshot = 0
    prompt_string = ''
    seqlen = 2048
    dl = InContextLearningSchemaTaskDataset(
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        max_seq_len=seqlen,
        pad_tok_id=tokenizer.eos_token_id,
        num_fewshot=num_fewshot,
        fewshot_random_seed=1,
        prompt_string=prompt_string,  # pyright: ignore
        example_delimiter='\n',  # pyright: ignore
        continuation_delimiter=' ### ',
        destination_path=str(
            tmp_path / 'test_human_eval_small.jsonl',
        ),  # pyright: ignore
    )
    example = {
        'context_options': ['context one', 'context two'],
        'gold': 0,
        'continuation': 'this is a continuation',
    }
    tokenized_example = dl.tokenize_example(
        prompt_and_fewshot='prompt ',
        context_options=example['context_options'],
        example=example,
    )
    assert all(
        tiny_gpt2_with_pad_tokenizer.decode(cont) == ' this is a continuation'
        for cont in tokenized_example['answer']
    )
    unpadded_inputs = [
        context[context != tokenizer.eos_token_id]
        for context in tokenized_example['context_options']
    ]
    untokenized_inputs = [
        tokenizer.decode(unpadded_input) for unpadded_input in unpadded_inputs
    ]
    assert untokenized_inputs == [
        'prompt context one this is a continuation',
        'prompt context two this is a continuation',
    ]


@pytest.mark.parametrize('dataset_uri', ['mmlu_small.jsonl'])
def test_mc_task_dataloader_subcategories(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 8
    seqlen = 64
    dls = get_icl_task_dataloader(
        'multiple_choice',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        has_categories=True,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'num_fewshot':
                2,
            'max_seq_len':
                seqlen,
            'pad_tok_id':
                tokenizer.eos_token_id,
            'prompt_string':
                'The following are multiple choice questions (with answers).\n',
            'example_delimiter':
                '\n',
            'continuation_delimiter':
                'Answer: ',
        },
    )
    assert isinstance(dls, dict)

    assert 'computer_security' in dls
    dl = dls['computer_security']
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())
    assert dl.dataloader.__len__() == 2
    assert 'input_ids' in batch
    assert tuple(batch['input_ids'].shape) == (batch_size, seqlen)
    assert 'attention_mask' in batch
    assert tuple(batch['attention_mask'].shape) == (batch_size, seqlen)
    assert 'continuation_indices' in batch
    assert isinstance(batch['continuation_indices'], list) and len(
        batch['continuation_indices'],
    ) == batch_size
    assert 'mode' in batch
    assert batch['mode'] == 'icl_task'
    min_idx = min(batch['continuation_indices'][0]).item()
    max_idx = max(batch['continuation_indices'][0]).item()
    assert tokenizer.decode(batch['input_ids'][0][min_idx:max_idx + 1]) == ' A'


@pytest.mark.parametrize('dataset_uri', [
    'pubmed_sm.jsonl',
])
def test_lm_task_dataloader_extra_space(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 2
    seqlen = 64
    dl = get_icl_task_dataloader(
        'language_modeling',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 10,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ' ',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())

    assert 'input_ids' in batch
    assert tuple(batch['input_ids'].shape) == (batch_size, seqlen)
    assert 'attention_mask' in batch
    assert tuple(batch['attention_mask'].shape) == (batch_size, seqlen)
    assert 'continuation_indices' in batch
    assert isinstance(batch['continuation_indices'], list) and len(
        batch['continuation_indices'],
    ) == batch_size
    assert 'mode' in batch
    assert batch['mode'] == 'icl_task'
    min_idx = min(batch['continuation_indices'][0]).item()
    max_idx = max(batch['continuation_indices'][0]).item()
    assert '  ' not in tokenizer.decode(batch['input_ids'][0][0:max_idx + 1])
    assert tokenizer.decode(
        batch['input_ids'][0][min_idx:max_idx + 1],
    ) == ' yes'


@pytest.mark.parametrize('dataset_uri', [
    'lambada_small.jsonl',
])
def test_lm_task_dataloader(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 2
    seqlen = 64
    dl = get_icl_task_dataloader(
        'language_modeling',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 0,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': '',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())

    assert 'input_ids' in batch
    assert tuple(batch['input_ids'].shape) == (batch_size, seqlen)
    assert 'attention_mask' in batch
    assert tuple(batch['attention_mask'].shape) == (batch_size, seqlen)
    assert 'continuation_indices' in batch
    assert isinstance(batch['continuation_indices'], list) and len(
        batch['continuation_indices'],
    ) == batch_size
    assert 'mode' in batch
    assert batch['mode'] == 'icl_task'
    min_idx = min(batch['continuation_indices'][0]).item()
    max_idx = max(batch['continuation_indices'][0]).item()
    assert tokenizer.decode(
        batch['input_ids'][0][min_idx:max_idx + 1],
    ) == ' glen'


@pytest.mark.parametrize('dataset_uri', ['winograd_small.jsonl'])
@pytest.mark.parametrize('prelimiter', ['', 'This is a question: '])
def test_schema_task_dataloader(
    dataset_uri: str,
    prelimiter: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 2
    seqlen = 64
    dl = get_icl_task_dataloader(
        'schema',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 1,
            'prompt_string': '',
            'example_delimiter': '\n',
            'prelimiter': prelimiter,
            'continuation_delimiter': '',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)
    batch = next(dl.dataloader._get_iterator())

    choices_per_question = 2
    assert 'input_ids' in batch
    assert tuple(batch['input_ids'].shape) == (batch_size, seqlen)
    assert 'attention_mask' in batch
    assert tuple(batch['attention_mask'].shape) == (batch_size, seqlen)
    assert 'continuation_indices' in batch
    assert isinstance(batch['continuation_indices'], list) and len(
        batch['continuation_indices'],
    ) == batch_size
    assert 'mode' in batch
    assert batch['mode'] == 'icl_task'
    assert 'gold_indices' in batch
    assert isinstance(batch['gold_indices'], list) and len(
        batch['gold_indices'],
    ) == batch_size // choices_per_question
    assert 'choice_groupings' in batch
    assert isinstance(batch['choice_groupings'], list) and len(
        batch['choice_groupings'],
    ) == batch_size // choices_per_question

    min_idx = min(batch['continuation_indices'][0]).item()
    max_idx = max(batch['continuation_indices'][0]).item()
    assert tokenizer.decode(
        batch['input_ids'][0][min_idx:max_idx + 1],
    ) == ' feared violence.'


@pytest.mark.parametrize('dataset_uri', ['winograd_small.jsonl'])
def test_schema_task_dataloader_sentpiece_tokenizer(
    dataset_uri: str,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        'huggyllama/llama-7b',  # type: ignore reportUnboundVariable
        use_fast=False,
    )
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 2
    seqlen = 64
    dl = get_icl_task_dataloader(
        'schema',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 1,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ' ',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)
    batch = next(dl.dataloader._get_iterator())

    choices_per_question = 2
    assert 'input_ids' in batch
    assert tuple(batch['input_ids'].shape) == (batch_size, seqlen)
    assert 'attention_mask' in batch
    assert tuple(batch['attention_mask'].shape) == (batch_size, seqlen)
    assert 'continuation_indices' in batch
    assert isinstance(batch['continuation_indices'], list) and len(
        batch['continuation_indices'],
    ) == batch_size
    assert 'mode' in batch
    assert batch['mode'] == 'icl_task'
    assert 'gold_indices' in batch
    assert isinstance(batch['gold_indices'], list) and len(
        batch['gold_indices'],
    ) == batch_size // choices_per_question
    assert 'choice_groupings' in batch
    assert isinstance(batch['choice_groupings'], list) and len(
        batch['choice_groupings'],
    ) == batch_size // choices_per_question

    max_idx = max(batch['continuation_indices'][0]).item()
    assert tokenizer.decode(
        batch['input_ids'][0][0:max_idx + 1],
    ) == "<s>The trophy doesn't fit into the brown suitcase because the suitcase is too small. \nThe city councilmen refused the demonstrators a permit because the city councilmen feared violence."


@pytest.mark.parametrize('dataset_uri', ['piqa_small.jsonl'])
@pytest.mark.parametrize('num_fewshot', [0, 1])
def test_mc_split_batch(
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
    dataset_uri: str,
    num_fewshot: int,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_tokenizer

    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 4
    seqlen = 512
    dl = get_icl_task_dataloader(
        'multiple_choice',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ': ',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())
    choices_per_question = 2
    real_microbatch_size = batch_size // 2
    logical_microbatch_size = real_microbatch_size // choices_per_question
    microbatches = dl.split_batch(batch, logical_microbatch_size)
    assert len(microbatches) == 2
    for i, microbatch in enumerate(microbatches):
        assert dl.get_num_samples_in_batch(microbatch) == 1
        assert 'input_ids' in microbatch
        assert tuple(
            microbatch['input_ids'].shape,
        ) == (real_microbatch_size, seqlen)
        assert 'attention_mask' in microbatch
        assert tuple(
            microbatch['attention_mask'].shape,
        ) == (real_microbatch_size, seqlen)
        assert 'continuation_indices' in microbatch
        assert isinstance(microbatch['continuation_indices'], list) and len(
            microbatch['continuation_indices'],
        ) == real_microbatch_size
        assert 'mode' in microbatch
        assert microbatch['mode'] == 'icl_task'
        assert 'gold_indices' in microbatch
        assert isinstance(microbatch['gold_indices'], list) and len(
            microbatch['gold_indices'],
        ) == real_microbatch_size // choices_per_question
        assert 'choice_groupings' in microbatch
        assert isinstance(microbatch['choice_groupings'], list) and len(
            microbatch['choice_groupings'],
        ) == real_microbatch_size // choices_per_question

        min_idx = min(microbatch['continuation_indices'][0]).item()
        max_idx = max(microbatch['continuation_indices'][0]).item()
        if i == 0:
            assert tokenizer.decode(
                microbatch['input_ids'][0][min_idx:max_idx + 1],
            ) == ' Pour it onto a plate'
        elif i == 1:
            assert tokenizer.decode(
                microbatch['input_ids'][0][min_idx:max_idx + 1],
            ) == ' Weld the metal together to get it to stay firmly in place'


@pytest.mark.parametrize('dataset_uri', ['triviaqa_small.jsonl'])
def test_qa_split_batch(
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
    dataset_uri: str,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/{dataset_uri}'
    tokenizer = tiny_gpt2_tokenizer

    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)  # for dist
    dl = get_icl_task_dataloader(
        icl_task_type='generation_task_with_answers',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=8,
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
        kwargs={
            'max_seq_len': 1024,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 0,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ': ',
        },
    )

    assert isinstance(dl, DataSpec)  # pyright

    batch = next(iter(dl.dataloader))
    split_batch = dl.split_batch(batch, 3)

    assert len(split_batch) == 2
    split1 = split_batch[0]
    split2 = split_batch[1]

    assert split1['input_ids'].shape[0] == 3
    assert split2['input_ids'].shape[0] == 1

    assert split1['attention_mask'].shape[0] == 3
    assert split2['attention_mask'].shape[0] == 1

    assert isinstance(split1['mode'], str)
    assert isinstance(split2['mode'], str)

    assert len(split1['labels']) == 3
    assert len(split2['labels']) == 1
    assert all(isinstance(v, list) for v in split1['labels'] + split2['labels'])

    assert isinstance(split1['generation_kwargs']['max_new_tokens'], int)
    assert isinstance(split2['generation_kwargs']['max_new_tokens'], int)

    assert isinstance(split1['generation_kwargs'], dict)
    assert isinstance(split2['generation_kwargs'], dict)


@pytest.mark.parametrize('dataset_uri', ['triviaqa_small.jsonl'])
@pytest.mark.parametrize('num_fewshot', [0])
@pytest.mark.parametrize('prompt_string', ['I am a prompt', ''])
def test_qa_task_dataloader_w_null_eos(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    num_fewshot: int,
    prompt_string: str,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 4
    seqlen = 512
    tiny_gpt2_with_pad_tokenizer.eos_token_id = None
    with pytest.raises(ValueError):
        _ = get_icl_task_dataloader(
            'generation_task_with_answers',
            dataset_uri,
            tokenizer,
            batch_size,
            destination_path=str(tmp_path / f'icl_{num_fewshot}.jsonl'),
            kwargs={
                'max_seq_len': seqlen,
                'pad_tok_id': tokenizer.eos_token_id,
                'num_fewshot': num_fewshot,
                'prompt_string': prompt_string,
                'example_delimiter': '\n',
                'prelimiter': 'Q: ',
                'continuation_delimiter': '\nA:',
            },
        )


@pytest.mark.parametrize('dataset_uri', ['triviaqa_small.jsonl'])
@pytest.mark.parametrize('num_fewshot', [0, 2])
@pytest.mark.parametrize('prompt_string', ['I am a prompt', ''])
def test_qa_task_dataloader(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    num_fewshot: int,
    prompt_string: str,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 4
    seqlen = 512
    # empirical number from the small test dataset
    maximum_answer_length = 7
    dl = get_icl_task_dataloader(
        'generation_task_with_answers',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / f'icl_{num_fewshot}.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': prompt_string,
            'example_delimiter': '\n',
            'prelimiter': 'Q: ',
            'continuation_delimiter': '\nA:',
        },
    )
    assert isinstance(dl, DataSpec)

    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())

    assert tuple(
        batch['input_ids'].shape,
    ) == (batch_size, seqlen - maximum_answer_length)
    assert tuple(
        batch['attention_mask'].shape,
    ) == (batch_size, seqlen - maximum_answer_length)
    assert batch['mode'] == 'generate'
    # the maximum generation length from the small test data

    assert batch['generation_kwargs']['max_new_tokens'] == maximum_answer_length
    assert all(item[0] == tokenizer.eos_token_id for item in batch['input_ids'])

    decoded_batch = tokenizer.batch_decode(batch['input_ids'])
    assert all(item.count('Q: ') == num_fewshot + 1 for item in decoded_batch)
    assert all(item.count('\nA:') == num_fewshot + 1 for item in decoded_batch)

    if len(prompt_string) > 0:
        assert all(item.count('I am a prompt') == 1 for item in decoded_batch)
    assert all(
        set(found) == set(expected) for found, expected in
        zip(batch['labels'], [['David Seville'], ['Skorpio', 'Scorpio']])
    )
    assert decoded_batch[0].endswith(
        'Q: Who was the man behind The Chipmunks?\nA:',
    )
    assert decoded_batch[1].endswith(
        'Q: What star sign is Jamie Lee Curtis?\nA:',
    )
    assert 'eos_token_id' in batch['generation_kwargs']


@pytest.mark.parametrize('dataset_uri', ['gsm8k_small.jsonl'])
@pytest.mark.parametrize('num_fewshot', [0, 2])
def test_qa_task_with_cot_dataloader(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    num_fewshot: int,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 2
    seqlen = 512
    # empirical number from the small test dataset
    maximum_answer_length = 132
    dl = get_icl_task_dataloader(
        'generation_task_with_answers',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / f'icl_{num_fewshot}.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'prelimiter': 'Q: ',
            'continuation_delimiter': "\nA: Let's think step by step. ",
            'cot_delimiter': ' #### ',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())
    assert tuple(
        batch['input_ids'].shape,
    ) == (batch_size, seqlen - maximum_answer_length)
    assert tuple(
        batch['attention_mask'].shape,
    ) == (batch_size, seqlen - maximum_answer_length)
    assert batch['mode'] == 'generate'
    # the maximum generation length from the small test data
    assert batch['generation_kwargs']['max_new_tokens'] == maximum_answer_length
    assert all(item[0] == tokenizer.eos_token_id for item in batch['input_ids'])
    decoded_batch = tokenizer.batch_decode(batch['input_ids'])
    assert all(item.count('Q: ') == num_fewshot + 1 for item in decoded_batch)
    assert all(item.count('\nA:') == num_fewshot + 1 for item in decoded_batch)

    assert batch['labels'] == [['18'], ['12334']]
    if num_fewshot == 0:
        assert decoded_batch[0].endswith(
            "Q: Janet’s ducks lay 16 eggs per day. She eats three for breakfast every morning and bakes muffins for her friends every day with four. She sells the remainder at the farmers' market daily for $2 per fresh duck egg. How much in dollars does she make every day at the farmers' market?\nA: Let's think step by step.",
        )
        assert decoded_batch[1].endswith(
            "Q: A robe takes 2 bolts of blue fiber and half that much white fiber.  How many bolts in total does it take?\nA: Let's think step by step.",
        )
    elif num_fewshot == 2:
        assert decoded_batch[0].endswith(
            "Q: Josh decides to try flipping a house.  He buys a house for $80,000 and then puts in $50,000 in repairs.  This increased the value of the house by 150%.  How much profit did he make?\nA: Let's think step by step. The cost of the house and repairs came out to 80,000+50,000=$<<80000+50000=130000>>130,000\nHe increased the value of the house by 80,000*1.5=<<80000*1.5=120000>>120,000\nSo the new value of the house is 120,000+80,000=$<<120000+80000=200000>>200,000\nSo he made a profit of 200,000-130,000=$<<200000-130000=70000>>70,000 #### 70000\nQ: James decides to run 3 sprints 3 times a week.  He runs 60 meters each sprint.  How many total meters does he run a week?\nA: Let's think step by step. He sprints 3*3=<<3*3=9>>9 times\nSo he runs 9*60=<<9*60=540>>540 meters #### 540\nQ: Janet’s ducks lay 16 eggs per day. She eats three for breakfast every morning and bakes muffins for her friends every day with four. She sells the remainder at the farmers' market daily for $2 per fresh duck egg. How much in dollars does she make every day at the farmers' market?\nA: Let's think step by step.",
        )
        assert decoded_batch[1].endswith(
            "Q: Janet’s ducks lay 16 eggs per day. She eats three for breakfast every morning and bakes muffins for her friends every day with four. She sells the remainder at the farmers' market daily for $2 per fresh duck egg. How much in dollars does she make every day at the farmers' market?\nA: Let's think step by step. Janet sells 16 - 3 - 4 = <<16-3-4=9>>9 duck eggs a day.\nShe makes 9 * 2 = $<<9*2=18>>18 every day at the farmer’s market. #### 18\nQ: Josh decides to try flipping a house.  He buys a house for $80,000 and then puts in $50,000 in repairs.  This increased the value of the house by 150%.  How much profit did he make?\nA: Let's think step by step. The cost of the house and repairs came out to 80,000+50,000=$<<80000+50000=130000>>130,000\nHe increased the value of the house by 80,000*1.5=<<80000*1.5=120000>>120,000\nSo the new value of the house is 120,000+80,000=$<<120000+80000=200000>>200,000\nSo he made a profit of 200,000-130,000=$<<200000-130000=70000>>70,000 #### 70000\nQ: A robe takes 2 bolts of blue fiber and half that much white fiber.  How many bolts in total does it take?\nA: Let's think step by step.",
        )


@pytest.mark.parametrize('dataset_uri', ['piqa_small.jsonl'])
@pytest.mark.parametrize('prelimiter', ['', 'This is a question: '])
def test_mc_task_dataloader(
    dataset_uri: str,
    prelimiter: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 2
    seqlen = 64
    example_delimiter = '\n'
    dl = get_icl_task_dataloader(
        'multiple_choice',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 1,
            'prompt_string': '',
            'prelimiter': prelimiter,
            'example_delimiter': example_delimiter,
            'continuation_delimiter': '\nA: ',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())

    choices_per_question = 2
    assert 'input_ids' in batch
    assert tuple(batch['input_ids'].shape) == (batch_size, seqlen)
    assert 'attention_mask' in batch
    assert tuple(batch['attention_mask'].shape) == (batch_size, seqlen)
    assert 'continuation_indices' in batch
    assert isinstance(batch['continuation_indices'], list) and len(
        batch['continuation_indices'],
    ) == batch_size
    assert 'mode' in batch
    assert batch['mode'] == 'icl_task'
    assert 'gold_indices' in batch
    assert isinstance(batch['gold_indices'], list) and len(
        batch['gold_indices'],
    ) == batch_size // choices_per_question
    assert 'choice_groupings' in batch
    assert isinstance(batch['choice_groupings'], list) and len(
        batch['choice_groupings'],
    ) == batch_size // choices_per_question

    min_idx = min(batch['continuation_indices'][0]).item()
    max_idx = max(batch['continuation_indices'][0]).item()
    assert tokenizer.decode(
        batch['input_ids'][0][min_idx:max_idx + 1],
    ) == ' Pour it onto a plate'
    q1 = 'how do you shake something?\nA: '
    a1 = 'move it up and down and side to side quickly.'
    q2 = "When boiling butter, when it's ready, you can\nA:"
    assert tokenizer.decode(
        batch['input_ids'][0][:min_idx],
    ) == f'{prelimiter}{q1}{a1}{example_delimiter}{prelimiter}{q2}'
    assert tokenizer.decode(
        batch['input_ids'][0][min_idx:max_idx + 1],
    ) == ' Pour it onto a plate'


@pytest.mark.parametrize('num_fewshot', [0, 5])
@pytest.mark.parametrize('dataset_uri', ['lambada_small.jsonl'])
# @pytest.mark.gpu
# @pytest.mark.world_size(2)
def test_lm_task_evaluation(
    num_fewshot: int,
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    tiny_gpt2_model: transformers.AutoModelForCausalLM,
):

    in_memory_logger = InMemoryLogger(
    )  # track the logged metrics in the in_memory_logger
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/{dataset_uri}'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 2
    dl = get_icl_task_dataloader(
        'language_modeling',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': 1024,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': '',
        },
    )

    evaluator = Evaluator(
        label='lambada',
        dataloader=dl,
        metric_names=['InContextLearningLMAccuracy'],
    )

    model = HuggingFaceModel(
        model=tiny_gpt2_model,
        tokenizer=tokenizer,
        eval_metrics=[InContextLearningLMAccuracy()],
        use_logits=True,
    )

    trainer = Trainer(model=model, max_duration='1ep', loggers=in_memory_logger)
    trainer.eval(eval_dataloader=evaluator, subset_num_batches=2)
    assert 'metrics/lambada/InContextLearningLMAccuracy' in in_memory_logger.data.keys(
    )
    assert in_memory_logger.data['metrics/lambada/InContextLearningLMAccuracy'][
        0][1].item() == 0


@pytest.mark.parametrize('num_fewshot', [0, 5])
@pytest.mark.parametrize('dataset_uri', ['winograd_small.jsonl'])
@pytest.mark.filterwarnings(r'ignore:Cannot split .* of length.*:UserWarning')
def test_schema_task_evaluation(
    num_fewshot: int,
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    tiny_gpt2_model: transformers.AutoModelForCausalLM,
):

    in_memory_logger = InMemoryLogger(
    )  # track the logged metrics in the in_memory_logger
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/{dataset_uri}'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 8
    dl = get_icl_task_dataloader(
        'schema',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': 1024,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ': ',
        },
    )

    evaluator = Evaluator(
        label='winograd',
        dataloader=dl,
        metric_names=['InContextLearningMultipleChoiceAccuracy'],
    )

    model = HuggingFaceModel(
        model=tiny_gpt2_model,
        tokenizer=tokenizer,
        eval_metrics=[InContextLearningMultipleChoiceAccuracy()],
        use_logits=True,
    )

    trainer = Trainer(model=model, max_duration='1ba', loggers=in_memory_logger)
    trainer.eval(eval_dataloader=evaluator)
    assert 'metrics/winograd/InContextLearningMultipleChoiceAccuracy' in in_memory_logger.data.keys(
    )
    assert in_memory_logger.data[
        'metrics/winograd/InContextLearningMultipleChoiceAccuracy'][0][1].item(
        ) > 0
    num_samples = 0
    with open(dataset_uri) as f:
        for _ in f:
            num_samples += 1
    assert trainer.state.eval_metrics['winograd'][
        'InContextLearningMultipleChoiceAccuracy'].total == num_samples


@pytest.mark.parametrize('dataset_uri', ['mmlu_small.jsonl'])
@pytest.mark.parametrize('num_fewshot', [0, 5])
@pytest.mark.gpu
@pytest.mark.world_size(2)
@pytest.mark.filterwarnings(r'ignore:Cannot split .* of length.*:UserWarning')
def test_mc_task_evaluation_subcategories(
    dataset_uri: str,
    num_fewshot: int,
    tiny_gpt2_model: transformers.AutoModelForCausalLM,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    in_memory_logger = InMemoryLogger(
    )  # track the logged metrics in the in_memory_logger
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/{dataset_uri}'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 16
    max_seq_len = 64
    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    reproducibility.seed_all(1234)
    dls = get_icl_task_dataloader(
        'multiple_choice',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
        has_categories=True,
        kwargs={
            'max_seq_len': max_seq_len,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ': ',
        },
    )

    assert isinstance(dls, dict)
    evaluators = [
        Evaluator(
            label='mmlu/' + k,
            dataloader=dl,
            metric_names=['InContextLearningMultipleChoiceAccuracy'],
        ) for k, dl in dls.items()
    ]

    model = HuggingFaceModel(
        model=tiny_gpt2_model,
        tokenizer=tiny_gpt2_with_pad_tokenizer,
        eval_metrics=[InContextLearningMultipleChoiceAccuracy()],
        use_logits=True,
    )

    trainer = Trainer(model=model, loggers=in_memory_logger)
    trainer.eval(eval_dataloader=evaluators)
    assert 'metrics/mmlu/computer_security/InContextLearningMultipleChoiceAccuracy' in in_memory_logger.data.keys(
    )
    assert in_memory_logger.data[
        'metrics/mmlu/computer_security/InContextLearningMultipleChoiceAccuracy'
    ][0][1].item() >= 0
    total = trainer.state.eval_metrics['mmlu/computer_security'][
        'InContextLearningMultipleChoiceAccuracy'].total
    dist.all_reduce(total)  # type: ignore
    assert total.item() == 4  # type: ignore


@pytest.mark.parametrize(
    'dataset_uri',
    ['piqa_small.jsonl', 'hellaswag_small.jsonl'],
)
@pytest.mark.parametrize('num_fewshot', [0, 5])
@pytest.mark.filterwarnings(r'ignore:Cannot split .* of length.*:UserWarning')
@pytest.mark.gpu
@pytest.mark.world_size(2)
def test_mc_task_evaluation(
    num_fewshot: int,
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    tiny_gpt2_model: transformers.AutoModelForCausalLM,
):

    in_memory_logger = InMemoryLogger(
    )  # track the logged metrics in the in_memory_logger
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/{dataset_uri}'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 8
    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)

    # seed because the fewshot selection is currently unseeded
    reproducibility.seed_all(1234)
    dl = get_icl_task_dataloader(
        'multiple_choice',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
        kwargs={
            'max_seq_len': 64,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ': ',
        },
    )

    evaluator = Evaluator(
        label='mc',
        dataloader=dl,
        metric_names=['InContextLearningMultipleChoiceAccuracy'],
    )

    model = HuggingFaceModel(
        model=tiny_gpt2_model,
        tokenizer=tiny_gpt2_with_pad_tokenizer,
        eval_metrics=[InContextLearningMultipleChoiceAccuracy()],
        use_logits=True,
    )

    trainer = Trainer(model=model, max_duration='1ba', loggers=in_memory_logger)
    trainer.eval(eval_dataloader=evaluator)
    assert 'metrics/mc/InContextLearningMultipleChoiceAccuracy' in in_memory_logger.data.keys(
    )
    assert in_memory_logger.data[
        'metrics/mc/InContextLearningMultipleChoiceAccuracy'][0][1].item() >= 0
    num_samples = 0
    with open(dataset_uri) as f:
        for _ in f:
            num_samples += 1
    total = trainer.state.eval_metrics['mc'][
        'InContextLearningMultipleChoiceAccuracy'].total
    dist.all_reduce(total)  # type: ignore
    assert total.item() == num_samples  # type: ignore


@pytest.mark.parametrize('dataset_uri', ['triviaqa_small.jsonl'])
@pytest.mark.parametrize('num_fewshot', [0, 5])
@pytest.mark.gpu
@pytest.mark.world_size(2)
@pytest.mark.filterwarnings(
    r'ignore:.*The dataloader_len \(2\) is greater than the length.*:UserWarning',
)
def test_qa_task_evaluation(
    num_fewshot: int,
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tiny_gpt2_model: transformers.AutoModelForCausalLM,
    tmp_path: Path,
):

    in_memory_logger = InMemoryLogger(
    )  # track the logged metrics in the in_memory_logger
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/{dataset_uri}'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 2
    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = get_icl_task_dataloader(
        'generation_task_with_answers',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
        kwargs={
            'max_seq_len': 1024,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ': ',
        },
    )

    evaluator = Evaluator(
        label='triviaqa',
        dataloader=dl,
        metric_names=['InContextLearningGenerationExactMatchAccuracy'],
    )

    model = HuggingFaceModel(
        model=tiny_gpt2_model,
        tokenizer=tiny_gpt2_with_pad_tokenizer,
        eval_metrics=[InContextLearningGenerationExactMatchAccuracy()],
        use_logits=True,
    )

    trainer = Trainer(model=model, max_duration='1ba', loggers=in_memory_logger)

    trainer.eval(eval_dataloader=evaluator, subset_num_batches=2)
    assert 'metrics/triviaqa/InContextLearningGenerationExactMatchAccuracy' in in_memory_logger.data.keys(
    )
    assert in_memory_logger.data[
        'metrics/triviaqa/InContextLearningGenerationExactMatchAccuracy'][0][
            1].item() == 0


@pytest.mark.parametrize('dataset_uri', ['gsm8k_small.jsonl'])
@pytest.mark.parametrize('num_fewshot', [5])
@pytest.mark.filterwarnings(
    r'ignore:.*The dataloader_len \(2\) is greater than the length.*:UserWarning',
)
@pytest.mark.gpu
@pytest.mark.world_size(2)
def test_qa_task_with_cot_evaluation(
    num_fewshot: int,
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tiny_gpt2_model: transformers.AutoModelForCausalLM,
    tmp_path: Path,
):

    in_memory_logger = InMemoryLogger(
    )  # track the logged metrics in the in_memory_logger
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')
    dataset_uri = f'{local_data}/{dataset_uri}'
    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 2
    tmp_path_to_broadcast = str(os.path.abspath(tmp_path))
    gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
    dl = get_icl_task_dataloader(
        'generation_task_with_answers',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(Path(gathered_paths[0]) / 'icl.jsonl'),
        kwargs={
            'max_seq_len': 1024,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': "A: Let's think step by step",
            'cot_delimiter': ' #### ',
        },
    )

    evaluator = Evaluator(
        label='gsm8k',
        dataloader=dl,
        metric_names=['InContextLearningGenerationExactMatchAccuracy'],
    )

    model = HuggingFaceModel(
        model=tiny_gpt2_model,
        tokenizer=tiny_gpt2_with_pad_tokenizer,
        eval_metrics=[InContextLearningGenerationExactMatchAccuracy()],
        use_logits=True,
    )

    trainer = Trainer(model=model, max_duration='1ba', loggers=in_memory_logger)

    trainer.eval(eval_dataloader=evaluator, subset_num_batches=2)
    assert 'metrics/gsm8k/InContextLearningGenerationExactMatchAccuracy' in in_memory_logger.data.keys(
    )
    assert in_memory_logger.data[
        'metrics/gsm8k/InContextLearningGenerationExactMatchAccuracy'][0][
            1].item() == 0


@pytest.mark.parametrize('dataset_uri', ['lambada_small.jsonl'])
def test_lm_spacing_dataloader(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
):

    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    tokenizer = tiny_gpt2_with_pad_tokenizer
    dataset_uri = f'{local_data}/{dataset_uri}'
    batch_size = 2
    seqlen = 512
    dl = get_icl_task_dataloader(
        'language_modeling',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'icl.jsonl'),
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 1,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ' UNIQUE ',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    first_batch = next(dl.dataloader._get_iterator())
    second_batch = next(dl.dataloader._get_iterator())

    first_batch_text = tokenizer.decode(
        first_batch['input_ids'][0],
        skip_special_tokens=True,
    )
    second_batch_text = tokenizer.decode(
        second_batch['input_ids'][0],
        skip_special_tokens=True,
    )

    first_batch_without_last_word = ' '.join(first_batch_text.split(' ')[:-1])
    second_batch_without_last_word = ' '.join(second_batch_text.split(' ')[:-1])

    assert first_batch_without_last_word.endswith(' UNIQUE')
    assert second_batch_without_last_word.endswith(' UNIQUE')

    assert first_batch_without_last_word.count(' UNIQUE ') == 1
    assert second_batch_without_last_word.count(' UNIQUE ') == 1


@pytest.mark.parametrize('dataset_uri', ['hf://mosaicml/test_dataset'])
@pytest.mark.parametrize('num_fewshot', [0, 1])
@pytest.mark.parametrize('prompt_string', ['Complete the voiceline: ', ''])
@pytest.mark.parametrize(
    'hf_loading_vars',
    [{
        'split': 'test',
        'name': 'juggernaut',
        'trust_remote_code': True,
    }],
)
@pytest.mark.parametrize(
    'hf_parsing_map',
    [None, {
        'context': ['context'],
        'continuation': ['continuation'],
    }],
)
@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_hf_dataloading_lm_dataloader(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    num_fewshot: int,
    prompt_string: str,
    hf_loading_vars: dict[str, str],
    hf_parsing_map: Optional[dict[str, list[str]]],
):

    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 2
    seqlen = 2048
    dl = get_icl_task_dataloader(
        'language_modeling',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': 0,
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ' ',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())

    assert 'input_ids' in batch
    assert tuple(batch['input_ids'].shape) == (batch_size, seqlen)
    assert 'attention_mask' in batch
    assert tuple(batch['attention_mask'].shape) == (batch_size, seqlen)
    assert 'continuation_indices' in batch
    assert isinstance(batch['continuation_indices'], list) and len(
        batch['continuation_indices'],
    ) == batch_size
    assert 'mode' in batch
    assert batch['mode'] == 'icl_task'
    min_idx = min(batch['continuation_indices'][0]).item()
    max_idx = max(batch['continuation_indices'][0]).item()
    assert tokenizer.decode(
        batch['input_ids'][0][min_idx:max_idx + 1],
    ) == ' and me.'

    decoded_batch = [
        tokenizer.decode(row[row != tokenizer.eos_token_id])
        for row in batch['input_ids']
    ]
    assert decoded_batch[0] == "Looks like it's just you and me."
    assert decoded_batch[
        1] == "There's a fine line between bravery and stupidity."


@pytest.mark.parametrize('dataset_uri', ['hf://mosaicml/test_dataset'])
@pytest.mark.parametrize('num_fewshot', [0, 1])
@pytest.mark.parametrize('prompt_string', ['What spell does this invoke? ', ''])
@pytest.mark.parametrize(
    'hf_loading_vars',
    [{
        'split': 'test',
        'name': 'invoker',
        'trust_remote_code': True,
    }],
)
@pytest.mark.parametrize(
    'hf_parsing_map',
    [{
        'context': ['quas', 'wex', 'exort'],
        'answer': ['spell'],
    }],
)
@pytest.mark.filterwarnings(
    r'ignore:The repository for mosaicml/test_dataset contains custom code which must*:FutureWarning',
)
def test_hf_dataloading_custom_parsing(
    dataset_uri: str,
    tiny_gpt2_with_pad_tokenizer: transformers.AutoTokenizer,
    tmp_path: Path,
    num_fewshot: int,
    prompt_string: str,
    hf_loading_vars: dict[str, str],
    hf_parsing_map: dict[str, list[str]],
):

    tokenizer = tiny_gpt2_with_pad_tokenizer
    batch_size = 2
    seqlen = 2048

    # empirical number from the small test dataset
    maximum_answer_length = 4

    dl = get_icl_task_dataloader(
        'generation_task_with_answers',
        dataset_uri=dataset_uri,
        tokenizer=tokenizer,
        batch_size=batch_size,
        destination_path=str(tmp_path / 'test_dataset_lm_juggernaut.jsonl'),
        hf_loading_vars=hf_loading_vars,
        hf_parsing_map=hf_parsing_map,
        kwargs={
            'max_seq_len': seqlen,
            'pad_tok_id': tokenizer.eos_token_id,
            'num_fewshot': num_fewshot,
            'prompt_string': prompt_string,
            'example_delimiter': '\n',
            'prelimiter': 'Orbs: ',
            'continuation_delimiter': '\nSpell:',
        },
    )
    assert isinstance(dl, DataSpec)
    assert isinstance(dl.dataloader, DataLoader)  # pyright
    batch = next(dl.dataloader._get_iterator())

    assert tuple(
        batch['input_ids'].shape,
    ) == (batch_size, seqlen - maximum_answer_length)
    assert tuple(
        batch['attention_mask'].shape,
    ) == (batch_size, seqlen - maximum_answer_length)
    assert batch['mode'] == 'generate'
    # the maximum generation length from the small test data
    assert batch['generation_kwargs']['max_new_tokens'] == maximum_answer_length
    assert all(item[0] == tokenizer.eos_token_id for item in batch['input_ids'])

    decoded_batch = tokenizer.batch_decode(batch['input_ids'])
    assert all(
        item.count('Orbs: ') == num_fewshot + 1 for item in decoded_batch
    )
    assert all(
        item.count('\nSpell:') == num_fewshot + 1 for item in decoded_batch
    )

    if len(prompt_string) > 0:
        assert all(
            item.count('What spell does this invoke? ') == 1
            for item in decoded_batch
        )
    assert all(
        set(found) == set(expected) for found, expected in
        zip(batch['labels'], [['defeaning blast'], ['cold snap']])
    )
    assert decoded_batch[0].endswith('Orbs: quas wex exort\nSpell:')
    assert decoded_batch[1].endswith('Orbs: quas quas quas\nSpell:')


@pytest.mark.parametrize(
    'prelimiter_key_name',
    ['prelimiter', 'question_prelimiter'],
)
def test_bc_question_prelimiter(
    mpt_tokenizer: transformers.PreTrainedTokenizerBase,
    prelimiter_key_name: str,
):
    local_data = os.path.join(os.path.dirname(__file__), 'local_data')

    dataset_uri = f'{local_data}/piqa_small.jsonl'

    icl_tasks = [
        {
            'dataset_uri': dataset_uri,
            'label': 'piqa',
            'icl_task_type': 'multiple_choice',
            'max_seq_len': 64,
            'pad_tok_id': mpt_tokenizer.eos_token_id,
            'num_fewshot': [0],
            'prompt_string': '',
            'example_delimiter': '\n',
            'continuation_delimiter': ': ',
            prelimiter_key_name: 'This is a question: ',
        },
    ]

    evaluators, _ = build_icl_evaluators(
        icl_tasks=icl_tasks,
        tokenizer=mpt_tokenizer,
        default_batch_size=2,
        default_max_seq_len=128,
    )

    assert len(evaluators) == 1
    evaluator = evaluators[0]
    assert evaluator.dataloader.dataloader.dataset.prelimiter == 'This is a question: '  # type: ignore


def test_icl_no_tokenizer():
    with pytest.raises(ValueError, match='Tokenizer is required for icl tasks'):
        _ = build_evaluators(
            eval_loader_config=None,
            icl_tasks_config=[],
            eval_gauntlet_config=None,
            tokenizer=None,
            device_eval_batch_size=2,
            icl_seq_len=128,
            icl_subset_num_batches=2,
        )
