# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

"""Build a StreamingTextDataset dataset and dataloader for training."""

import inspect
from itertools import islice
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Sequence,
    Union,
    cast,
)

import numpy as np
import torch
from composer.core.data_spec import DataSpec
from streaming import Stream, StreamingDataset
from torch.utils.data import DataLoader
from transformers import PreTrainedTokenizerBase

from llmfoundry import registry
from llmfoundry.data.data import (
    SUPPORTED_MDS_ENCODING_TYPES,
    stream_remote_local_validate,
)
from llmfoundry.utils.registry_utils import construct_from_registry

__all__ = [
    'StreamingTextDataset',
    'build_text_dataloader',
    'ConcatenatedSequenceCollatorWrapper',
]


class StreamingTextDataset(StreamingDataset):
    """Generic text dataset using MosaicML's StreamingDataset.

    Args:
        tokenizer (Tokenizer): HuggingFace tokenizer to
            tokenize samples.
        max_seq_len (int): The max sequence length of each sample.
        token_encoding_type (str): The encoding type of the tokenized samples. This is only used
            for legacy datasets that have been written directly as 'bytes' instead of numpy
            arrays. Types are auto-inferred for numpy arrays. Defaults to 'int64'.
        streams (Sequence[Stream], optional): One or more Streams to stream/cache samples from,
            which may be upsampled or downsampled. StreamingDataset uses either ``streams`` or
            ``remote``/``local``. Defaults to ``None``.
        remote (str, optional): Remote path or directory to download the dataset from. If ``None``,
            its data must exist locally. StreamingDataset uses either ``streams`` or
            ``remote``/``local``. Defaults to ``None``.
        local (str, optional): Local working directory to download shards to. This is where shards
            are cached while they are being used. Uses a temp directory if not set.
            StreamingDataset uses either ``streams`` or ``remote``/``local``. Defaults to ``None``.
        split (str, optional): Which dataset split to use, if any. If provided, we stream from/to
            the ``split`` subdirs of  ``remote`` and ``local``. Defaults to ``None``.
        download_retry (int): Number of download re-attempts before giving up. Defaults to ``2``.
        download_timeout (float): Number of seconds to wait for a shard to download before raising
            an exception. Defaults to ``60``.
        validate_hash (str, optional): Optional hash or checksum algorithm to use to validate
            shards. Defaults to ``None``.
        keep_zip (bool): Whether to keep or delete the compressed form when decompressing
            downloaded shards. If ``False``, keep iff remote is local or no remote. Defaults to
            `False``.
        epoch_size (Union[int, str], optional): Number of samples to draw per epoch balanced across all
            streams. If ``None``, takes its value from the total number of underlying samples.
            Provide this field if you are weighting streams relatively to target a larger or
            smaller epoch size. Defaults to ``None``.
        predownload (int, optional): Target number of samples ahead to download the shards of while
            iterating. If ``None``, its value is set to ``8 * batch_size``. Defaults to ``None``.
        cache_limit (Union[int, str], optional) - Maximum size in bytes of this StreamingDataset's
            shard cache. Before downloading a shard, the least recently used resident shard(s) may
            be evicted (deleted from the local cache) in order to stay under the limit. Set to None
            to disable shard eviction. Supports integer bytes as well as string human-readable
            bytes (e.g., 100b, 64kb, 77mb, and so on). Defaults to None.
        partition_algo (str): Which partitioning algorithm to use. Defaults to ``orig``.
        num_canonical_nodes (int, optional): Canonical number of nodes for shuffling with
            resumption. If ``None``, this is interpreted as 64 times the number of physical
            nodes of the initial run if ``shuffle_algo`` is ``py1s`` or ``py2s``, and simply the
            number of physical nodes of the initial run otherwise. Defaults to ``None``.
        batch_size (int, optional): Batch size of its DataLoader, which affects how the dataset is
            partitioned over the workers. Defaults to ``None``.
        shuffle (bool): Whether to iterate over the samples in randomized order. Defaults to
            ``False``.
        shuffle_algo (str): Which shuffling algorithm to use. Defaults to ``py1e``.
        shuffle_seed (int): Seed for Deterministic data shuffling. Defaults to ``9176``.
        shuffle_block_size (int, optional): Unit of shuffle. A canonical node's samples are split
            into blocks of this size, and samples within each block are shuffled. If ``None``, its
            value is calculated as ``max(4_000_000 // num_canonical_nodes), 1 << 18)``. Defaults to
            ``None``.
        sampling_method (str): Which sampling method to use, either ``balanced`` or ``fixed``.
            Defaults to ``balanced``.
        sampling_granularity (int): When picking samples for a stream's final partial repeat,
            how many samples to pick from the same shard at a time (``1`` for evenly balanced
            across shards, ``1000`` to pick 1000 samples from the same shard at a time, etc).
            Defaults to ``1``.
        batching_method (str): Which batching method to use, either ``random``, ``stratified``, or
            ``per_stream``. Defaults to ``random``.
        allow_unsafe_types (bool): If a shard contains Pickle, which allows arbitrary code
            execution during deserialization, whether to keep going if ``True`` or raise an error
            if ``False``. Defaults to ``False``.
        replication (int, optional): Determines how many consecutive devices will receive the same
            samples. Useful for training with tensor or sequence parallelism, where multiple
            devices need to see the same partition of the dataset. Defaults to ``None``.
        stream_name (str): The name of the Stream to use which is registered in
            streaming.base.stream.streams_registry. Defaults to ``stream``.
        stream_config (dict[str, Any]): Additional arguments to pass to the Stream constructor.
    """

    def __init__(
        self,
        tokenizer: PreTrainedTokenizerBase,
        max_seq_len: int,
        token_encoding_type: str = 'int64',
        streams: Optional[Sequence[Stream]] = None,
        remote: Optional[str] = None,
        local: Optional[str] = None,
        split: Optional[str] = None,
        download_retry: int = 2,
        download_timeout: float = 60,
        validate_hash: Optional[str] = None,
        keep_zip: bool = False,
        epoch_size: Optional[Union[int, str]] = None,
        predownload: Optional[int] = None,
        cache_limit: Optional[Union[int, str]] = None,
        partition_algo: str = 'relaxed',
        num_canonical_nodes: Optional[int] = None,
        batch_size: Optional[int] = None,
        shuffle: bool = False,
        shuffle_algo: str = 'py1e',
        shuffle_seed: int = 9176,
        shuffle_block_size: Optional[int] = None,
        sampling_method: str = 'balanced',
        sampling_granularity: int = 1,
        batching_method: str = 'random',
        allow_unsafe_types: bool = False,
        replication: Optional[int] = None,
        stream_name: str = 'stream',
        stream_config: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ):

        if token_encoding_type not in SUPPORTED_MDS_ENCODING_TYPES:
            raise ValueError(
                f'The token_encoding_type must be one of {SUPPORTED_MDS_ENCODING_TYPES}, but got {token_encoding_type}',
            )
        self.token_encoding_type = token_encoding_type

        if streams is None:
            stream_remote_local_validate(remote, local, split)
        else:
            for stream in streams:
                stream_remote_local_validate(
                    stream.remote,
                    stream.local,
                    split,
                )

        # TODO: discover where yamls are being converted incorrect, but temporary workaround
        if isinstance(shuffle_block_size, float):
            shuffle_block_size = int(shuffle_block_size)

        # Build Dataset
        super().__init__(
            streams=streams,
            remote=remote,
            local=local,
            split=split,
            download_retry=download_retry,
            download_timeout=download_timeout,
            validate_hash=validate_hash,
            keep_zip=keep_zip,
            epoch_size=epoch_size,
            predownload=predownload,
            cache_limit=cache_limit,
            partition_algo=partition_algo,
            num_canonical_nodes=num_canonical_nodes,
            batch_size=batch_size,
            shuffle=shuffle,
            shuffle_algo=shuffle_algo,
            shuffle_seed=shuffle_seed,
            shuffle_block_size=shuffle_block_size,
            sampling_method=sampling_method,
            sampling_granularity=sampling_granularity,
            batching_method=batching_method,
            allow_unsafe_types=allow_unsafe_types,
            replication=replication,
            stream_name=stream_name,
            stream_config=stream_config,
            **kwargs,
        )
        self.tokenizer = tokenizer
        self.max_seq_len = max_seq_len

    # How to tokenize a text sample to a token sample
    def _tokenize(self, text_sample: Mapping) -> dict[str, list[int]]:
        if self.tokenizer.pad_token is None:
            # Some tokenizers (e.g. GPT2 tokenizer) have no padding token which causes bugs
            raise RuntimeError(
                'If tokenizing on-the-fly, tokenizer must have a pad_token_id',
            )

        return self.tokenizer(
            text_sample['text'],
            truncation=True,
            padding='max_length',
            max_length=self.max_seq_len,
        )

    def _read_binary_tokenized_sample(
        self,
        sample: dict[str, Any],
    ) -> torch.Tensor:
        # Modeling code still expects int64 tensors.
        if isinstance(sample['tokens'], np.ndarray):
            return torch.from_numpy(
                sample['tokens'][:self.max_seq_len].copy(),
            ).to(torch.int64)
        else:
            return torch.from_numpy(
                np.frombuffer(
                    sample['tokens'],
                    dtype=getattr(np, self.token_encoding_type),
                )[:self.max_seq_len].copy(),
            ).to(torch.int64)

    # How to process a sample
    def __getitem__(self,
                    idx: int) -> Union[dict[str, list[int]], torch.Tensor]:
        sample = super().__getitem__(idx)
        if 'text' in sample:
            token_sample = self._tokenize(sample)
        elif 'tokens' in sample:
            token_sample = self._read_binary_tokenized_sample(sample)
        else:
            raise RuntimeError(
                'StreamingTextDataset needs samples to have a `text` or `tokens` column',
            )
        return token_sample


class ConcatenatedSequenceCollatorWrapper:
    """Collator wrapper to add sequence_id to batch."""

    def __init__(
        self,
        base_collator: Callable,
        eos_token_id: Optional[int] = None,
        bos_token_id: Optional[int] = None,
    ):
        self.base_collator = base_collator
        if (eos_token_id is None) and (bos_token_id is None):
            raise ValueError(
                'Must supply a value for either eos_token_id or bos_token_id, but got None for both.',
            )
        if (eos_token_id is not None) and (bos_token_id is not None):
            raise ValueError(
                'Cannot use *both* EOS and BOS tokens for detecting sequence boundaries. ' +\
                'Please supply `eos_token_id` if sequences end with an EOS token, or use ' +\
                '`bos_token_id` if sequences start with a BOS token.',
            )

        if eos_token_id is None:
            self.split_token_id = cast(int, bos_token_id)
            self.bos_mode = True
        else:
            self.split_token_id = eos_token_id
            self.bos_mode = False

    def __call__(self, examples: list[Any]) -> dict[str, torch.Tensor]:
        batch = self.base_collator(examples)
        batch['sequence_id'] = self.get_sequence_id_from_batch(batch)
        return batch

    def get_sequence_id_from_batch(
        self,
        batch: dict[str, torch.Tensor],
    ) -> torch.Tensor:
        is_separator = torch.eq(batch['input_ids'], self.split_token_id)
        cumulative_sep = torch.cumsum(is_separator,
                                      dim=1).to(batch['input_ids'].dtype)
        # If separator token is bos, we're already done
        if self.bos_mode:
            return cumulative_sep

        # If separator token is eos, right shift 1 space
        left_zeros = cumulative_sep.new_zeros((cumulative_sep.shape[0], 1))
        return torch.cat([left_zeros, cumulative_sep[:, :-1]], dim=1)


def build_streams(streams: Optional[dict[str, Any]] = None,):
    streams_dict = streams
    # build streams
    streams_ret = []
    if streams_dict is not None:
        streams_ret = [Stream(**stream) for stream in streams_dict.values()]
    return streams_ret


def build_text_dataloader(
    tokenizer: Optional[PreTrainedTokenizerBase],
    device_batch_size: Union[int, float],
    dataset: dict[str, Any],
    drop_last: bool,
    num_workers: int,
    pin_memory: bool = True,
    prefetch_factor: int = 2,
    persistent_workers: bool = True,
    timeout: int = 0,
) -> DataSpec:
    if tokenizer is None:
        raise ValueError('Tokenizer is required for text dataloader')

    dataset_cfg = dataset

    # get kwargs
    dataset_cfg['replication'], dataset_batch_size = construct_from_registry(
        name='dataset_replication_validator',
        registry=registry.dataset_replication_validators,
        partial_function=False,
        kwargs={
            'dataset_cfg': dataset_cfg,
            'tokenizer': tokenizer,
            'device_batch_size': device_batch_size,
        },
    )

    streams = build_streams(
        streams=dataset_cfg.pop('streams')
        if 'streams' in dataset_cfg else None,
    )

    valid_streaming_text_dataset_parameters = inspect.signature(
        StreamingTextDataset,
    ).parameters

    valid_base_dataset_params = inspect.signature(StreamingDataset,).parameters

    dataset_config_subset_for_streaming_text_dataset = {
        k: v
        for k, v in dataset_cfg.items()
        if k in valid_streaming_text_dataset_parameters or
        k in valid_base_dataset_params
    }

    # build dataset potentially with streams
    text_dataset = StreamingTextDataset(
        tokenizer=tokenizer,
        streams=streams,
        batch_size=dataset_batch_size,
        **dataset_config_subset_for_streaming_text_dataset,
    )

    dataloader_cfg = {
        'name': 'text',
        'dataset': dataset_cfg,
        'drop_last': drop_last,
        'num_workers': num_workers,
        'pin_memory': pin_memory,
        'prefetch_factor': prefetch_factor,
        'persistent_workers': persistent_workers,
        'timeout': timeout,
    }

    collate_fn, dataloader_batch_size = construct_from_registry(
        name='text_collator',
        registry=registry.collators,
        partial_function=False,
        kwargs={
            'dataloader_cfg': dataloader_cfg,
            'tokenizer': tokenizer,
            'dataset_batch_size': dataset_batch_size,
        },
    )

    dl = DataLoader(
        text_dataset,
        collate_fn=collate_fn,
        batch_size=dataloader_batch_size,
        drop_last=drop_last,
        num_workers=num_workers,
        pin_memory=pin_memory,
        prefetch_factor=prefetch_factor,
        persistent_workers=persistent_workers,
        timeout=timeout,
    )

    return construct_from_registry(
        name='data_spec',
        registry=registry.data_specs,
        partial_function=False,
        kwargs={
            'dl': dl,
            'dataset_cfg': dataset_cfg,
        },
    )


# Helpful to test if your dataloader is working locally
# Run `python data.py  --local_path [local] [--remote_path remote, optional]` and verify that batches are printed out
if __name__ == '__main__':
    import argparse

    from llmfoundry.utils.builders import build_tokenizer

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--tokenizer',
        type=str,
        default='EleutherAI/gpt-neox-20b',
        help='the name of the tokenizer to use',
    )
    parser.add_argument(
        '--local_path',
        type=str,
        required=True,
        help='the path to the local copy of the dataset',
    )
    parser.add_argument(
        '--remote_path',
        type=str,
        default=None,
        help='the path to the remote copy to stream from (optional)',
    )
    parser.add_argument(
        '--split',
        type=str,
        default='val',
        help='which split of the dataset to use',
    )
    parser.add_argument(
        '--max_seq_len',
        type=int,
        default=32,
        help='max sequence length to test',
    )

    args = parser.parse_args()

    if args.remote_path is not None:
        print(
            f'Reading {args.split} split from {args.local_path} <- streamed from <- {args.remote_path}',
        )
    else:
        print(f'Reading {args.split} split from {args.local_path}')

    cfg = {
        'dataset': {
            'local': args.local_path,
            'remote': args.remote_path,
            'split': args.split,
            'shuffle': False,
            'max_seq_len': args.max_seq_len,
            'keep_zip': True,  # in case we need compressed files after testing
        },
        'drop_last': False,
        'num_workers': 4,
    }
    device_batch_size = 2

    tokenizer_name = args.tokenizer
    tokenizer_kwargs = {'model_max_length': args.max_seq_len}
    tokenizer = build_tokenizer(tokenizer_name, tokenizer_kwargs)

    loader = build_text_dataloader(
        **cfg,
        tokenizer=tokenizer,
        device_batch_size=device_batch_size,
    ).dataloader
    assert isinstance(loader, DataLoader)
    assert isinstance(loader.dataset, StreamingTextDataset)
    tokenizer = loader.dataset.tokenizer

    for batch_ix, batch in enumerate(islice(loader, 5)):
        print('\n')
        print('#' * 20, f'Batch {batch_ix}', '#' * 20)
        for k, v in batch.items():
            if isinstance(v, torch.Tensor):
                print(k, v.shape, v.dtype)
            else:
                print(k, v)
        for sample_ix, token_sample in enumerate(batch['input_ids']):
            print('-' * 20, f' Sample {sample_ix} ', '-' * 20)
            print(tokenizer.decode(token_sample))
