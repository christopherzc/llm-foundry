# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

import logging
import tempfile
from typing import Any, Callable, Iterable, Literal, Optional

import numpy as np
import torch
from composer.utils import dist
from transformers import PreTrainedTokenizerBase

from llmfoundry.utils.consts import CROSS_ENTROPY_IGNORE_INDEX

log = logging.getLogger(__name__)

__all__ = [
    'BinPackCollator',
    'auto_packing_ratio',
    'profile_packing',
]


class BinPackCollator:
    """Utility collator for packing to reduce padding.

    Args:
        collator (Callable): The base collator to use.
        target_batch_size (int): The number of bins.
        max_seq_len(int): The maximum sequence length of a bin.
        pad_token_id (int): The padding token id.
        padding_side (Literal['left', 'right']): The side to pad on.
        max_leftover_bins_to_keep (Optional[int]): The number of leftover bins
            to keep.
        is_profiling (bool): Whether the collator is being used for profiling.
            In profiling mode, packing and padding the example tensors is
            avoided for efficiency, and the returned batch will be None.
    """

    def __init__(
        self,
        collator: Callable,
        target_batch_size: int,
        max_seq_len: int,
        pad_token_id: int,
        padding_side: Literal['left', 'right'],
        max_leftover_bins_to_keep: Optional[int] = None,
        is_profiling: bool = False,
    ):
        self.base_collator = collator
        self.out_size = int(target_batch_size)
        self.max_seq_len = int(max_seq_len)
        self.pad_token_id = int(pad_token_id)
        self.padding_side = padding_side

        if self.out_size <= 0:
            raise ValueError(f'{target_batch_size=} must be >0.')
        if self.max_seq_len <= 0:
            raise ValueError(f'{max_seq_len=} must be >0.')
        if self.pad_token_id < 0:
            raise ValueError(f'{pad_token_id=} must be >=0.')

        if max_leftover_bins_to_keep is not None and max_leftover_bins_to_keep < 0:
            raise ValueError(
                f'{max_leftover_bins_to_keep=} must be >=0 or None.',
            )
        self.max_leftover_bins_to_keep = max_leftover_bins_to_keep

        self.n_packed_tokens = 0
        self.n_total_tokens = 0
        self.n_packed_examples = 0

        self._leftover_bins: list[tuple[int, dict[str, torch.Tensor]]] = []

        self._is_profiling = is_profiling

    @property
    def waste(self) -> float:
        return 1 - (self.n_packed_tokens / self.n_total_tokens)

    @property
    def efficiency(self) -> float:
        return self.n_packed_tokens / (
            self.max_seq_len * self.n_packed_examples
        )

    def __call__(
        self,
        examples: list[dict[str, torch.Tensor]],
    ) -> dict[str, torch.Tensor]:
        batch = self.base_collator(examples)
        return self.pack(batch)

    def pack(self, batch: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        if self._is_profiling:
            raise ValueError('Cannot pack in profiling mode.')

        assert 'attention_mask' in batch
        assert 'input_ids' in batch

        for key in batch.keys():
            assert key in [
                'input_ids',
                'labels',
                'attention_mask',
                'sequence_id',
            ]
        # Cut everything down to size
        sizes, trimmed_examples = self._trim_batch(batch)
        packed_batch = self._pack_trimmed_examples(trimmed_examples, sizes)
        assert packed_batch is not None
        return packed_batch

    def _pack_trimmed_examples(
        self,
        trimmed_examples: list[dict[str, torch.Tensor]],
        sizes: list[int],
    ) -> Optional[dict[str, torch.Tensor]]:
        """Packs trimmed examples into fixed-size bins and repads them.

        Args:
            trimmed_examples (List[Dict[str, torch.Tensor]]): A list of trimmed examples.
            sizes (List[int]): The sizes of the trimmed examples.

        Returns:
            Optional[Dict[str, torch.Tensor]]: A batch of repadded examples
                ready for processing. If the collator is in profiling mode,
                returns None.
        """
        # Apply our CS 101 bin packing algorithm.
        packed_examples, n_packed_tokens, n_total_tokens, leftover_bins = self._first_fit_bin_packing(
            sizes=sizes,
            examples=trimmed_examples,
            num_bins=self.out_size,
            max_bin_size=self.max_seq_len,
            existing_bins=self._leftover_bins,
        )
        self.n_packed_tokens += n_packed_tokens
        self.n_total_tokens += n_total_tokens
        self.n_packed_examples += self.out_size
        self._leftover_bins = leftover_bins[:self.max_leftover_bins_to_keep]

        if self._is_profiling:
            return None
        # Re-pad to max_seq_len and batch
        batch = self._convert_to_batch(packed_examples)
        return batch

    def _convert_to_batch(
        self,
        packed_examples: list[dict[str, torch.Tensor]],
    ) -> dict[str, torch.Tensor]:

        pad_vals = {
            'input_ids': self.pad_token_id,
            'labels': CROSS_ENTROPY_IGNORE_INDEX,
            'attention_mask': 0,
            'sequence_id': -1,
        }
        keys = packed_examples[0].keys()
        batch = {}
        for key in keys:
            batch[key] = torch.stack([
                _pad_tensor(
                    example[key],
                    pad_vals[key],
                    self.max_seq_len,
                    self.padding_side,
                ) for example in packed_examples
            ])
        return batch

    def _first_fit_bin_packing(
        self,
        sizes: list[int],
        examples: list[dict[str, torch.Tensor]],
        num_bins: int,
        max_bin_size: int,
        existing_bins: list[tuple[int, dict[str, torch.Tensor]]],
    ) -> tuple[list[dict[str, torch.Tensor]], int, int, list[tuple[int, dict[
        str, torch.Tensor]]]]:

        # Will contain tuples (bin_size_size, packed_example)
        bins: list[tuple[int, dict[str, torch.Tensor]]] = existing_bins

        starting_total_bin_sizes = sum([bin_size for bin_size, _ in bins])

        sizes_and_examples = list(zip(sizes, examples))
        sorted_sizes_and_examples = sorted(
            sizes_and_examples,
            key=lambda x: x[0],
            reverse=True,
        )

        required_num_examples = max(0, num_bins - len(bins))
        num_examples = len(sizes)
        if num_examples < required_num_examples:
            for size, example in sorted_sizes_and_examples:
                # Can't keep packing. All remaining items get their own bin.
                bins.append((size, example))

            total_bin_sizes = sum([bin_size for bin_size, _ in bins])
            total_new_bin_sizes = total_bin_sizes - starting_total_bin_sizes
            total_example_sizes = sum(sizes)
            if total_new_bin_sizes != total_example_sizes:
                raise AssertionError(
                    f'Error in packing. {total_example_sizes=} does not equal {total_new_bin_sizes=}.',
                )

            sorted_bins = sorted(bins, key=lambda x: x[0], reverse=True)
            bin_sizes, packed_examples = [], []
            for bin_size, packed_example in sorted_bins:
                bin_sizes.append(bin_size)
                packed_examples.append(packed_example)

            # Return:
            #  - the num_bins largest packed examples
            #  - the total tokens in those examples
            #  - the total size of all new examples
            #  - leftover bins
            return packed_examples[:num_bins], sum(
                bin_sizes[:num_bins],
            ), sum(sizes), sorted_bins[num_bins:]

        # Go through each item from longest to shortest.
        # Note: all items will either go into an existing or new bin.
        for i, (size, example) in enumerate(sorted_sizes_and_examples):
            # If we can't keep packing, all remaining items get their own bin.
            required_num_examples = max(0, num_bins - len(bins))
            n_remaining = num_examples - i
            assert n_remaining >= required_num_examples
            if n_remaining == required_num_examples:
                # Can't keep packing. All remaining items get their own bin.
                bins.append((size, example))
                continue

            # Add it to the first bin it fits in
            added = False
            for bidx in range(len(bins)):
                if bins[bidx][0] + size <= max_bin_size:
                    bin_size, packed_example = bins.pop(bidx)
                    bin_size = bin_size + size
                    # When profiling, don't combine the tensor for efficiency
                    # and return the first example which isn't "correct"
                    if not self._is_profiling:
                        packed_example = _combine_in_place(
                            packed_example,
                            example,
                        )

                    bins.append((bin_size, packed_example))
                    added = True
                    break
            # If it didn't fit anywhere, open a new bin
            if not added:
                bins.append((size, example))

        total_bin_sizes = sum([bin_size for bin_size, _ in bins])
        total_new_bin_sizes = total_bin_sizes - starting_total_bin_sizes
        total_example_sizes = sum(sizes)
        if total_new_bin_sizes != total_example_sizes:
            raise AssertionError(
                f'Error in packing. {total_example_sizes=} does not equal {total_new_bin_sizes=}.',
            )

        sorted_bins = sorted(bins, key=lambda x: x[0], reverse=True)
        bin_sizes, packed_examples = [], []
        for bin_size, packed_example in sorted_bins:
            bin_sizes.append(bin_size)
            packed_examples.append(packed_example)

        # Return:
        #  - the num_bins largest packed examples
        #  - the total tokens in those examples
        #  - the total size of all new examples
        #  - leftover bins
        return packed_examples[:num_bins], sum(
            bin_sizes[:num_bins],
        ), sum(sizes), sorted_bins[num_bins:]

    @classmethod
    def _trim_batch(
        cls,
        batch: dict[str, torch.Tensor],
    ) -> tuple[list[int], list[dict[str, torch.Tensor]]]:
        """Trims padding off all examples in batch.

        Args:
            batch (Dict[str, torch.Tensor]): Batch of padded data with tensors as values.

        Returns:
            A tuple with unpadded lengths of examples and a list of each trimmed example from the batch.
        """
        # Cut everything down to size
        sizes, trimmed_examples = [], []
        for idx in range(batch['attention_mask'].shape[0]):
            size, trimmed_example = cls._extract_trim_batch_idx(
                batch,
                idx,
            )
            sizes.append(size)
            trimmed_examples.append(trimmed_example)
        return sizes, trimmed_examples

    @classmethod
    def _extract_trim_batch_idx(
        cls,
        batch: dict[str, torch.Tensor],
        idx: int,
    ) -> tuple[int, dict[str, torch.Tensor]]:
        example = {k: v[idx] for k, v in batch.items()}

        keep = example['attention_mask'] == 1
        size = int(keep.sum())
        trim_example = {k: v[keep] for k, v in example.items()}
        trim_example['sequence_id'] = torch.zeros_like(
            trim_example['input_ids'],
        )

        return size, trim_example


def _combine_in_place(
    example: dict[str, torch.Tensor],
    add_on: dict[str, torch.Tensor],
) -> dict[str, torch.Tensor]:
    if 'labels' in add_on:
        # Prevents the last token in example from being trained to
        # predict the first token in add_on, which would make no sense.
        add_on['labels'][0] = CROSS_ENTROPY_IGNORE_INDEX

    for k in example.keys():
        if k == 'sequence_id':
            example[k] = torch.cat([
                example[k],
                add_on[k] + 1 + torch.max(example[k]),
            ])
        else:
            example[k] = torch.cat([example[k], add_on[k]])
    return example


def _pad_tensor(
    tensor: torch.Tensor,
    pad_value: int,
    max_seq_len: int,
    padding_side: str,
) -> torch.Tensor:
    if len(tensor) == max_seq_len:
        return tensor
    t = torch.full((max_seq_len,),
                   pad_value,
                   dtype=tensor.dtype,
                   device=tensor.device)
    if padding_side == 'left':
        t[-len(tensor):] = tensor
    elif padding_side == 'right':
        t[:len(tensor)] = tensor
    else:
        raise ValueError(f'Unknown {padding_side=}')
    return t


def auto_packing_ratio(
    dataloader_cfg: dict[str, Any],
    tokenizer: PreTrainedTokenizerBase,
    device_batch_size: int,
    num_packing_ratios: int = 20,
    max_ratio: Optional[float] = None,
    packing_collator_cls: type[BinPackCollator] = BinPackCollator,
    inner_collator: Callable = lambda x: x,
) -> float:
    """Find a packing ratio that minimizes padding with zero waste.

    By packing examples, we can increase training efficiency, training on more data with less batches.
    However, in practice, the selected packing_ratio may produce some waste because profiling is done on only
    a subset of the dataset.

    We select a min_ratio of 1 and a max_ratio that is the max_seq_len / 100, and profile up to
    num_packing_ratios packing ratios between min_ratio and max_ratio, inclusive.
    When a packing_ratio with non-zero waste is found, we stop and select the previous ratio,
    which has zero waste.

    Args:
        dataloader_cfg (DictConfig): The dataloader configuration for profiling.
        tokenizer (PreTrainedTokenizerBase): The tokenizer for profiling.
        device_batch_size (int): The size of the batches (number of examples) per device.
        num_packing_ratios (int): The number of packing ratios to try.
        max_ratio (Optional[float]): The largest packing ratio to try. Must be larger than `min_ratio`.
            Defaults to max_seq_len / 100.
        packing_collator_cls (BinPackCollator): The collator class to use for packing.
        inner_collator (Callable): The inner collator to use by the packing collator.

    Returns:
        A packing ratio that minimizes padding while maintaining zero waste.
    """
    from composer.utils import dist, get_device, reproducibility

    log.debug('Searching for optimal packing ratio.')

    # Stash the rng state to restore later.
    rng_state = reproducibility.get_rng_state()
    # Set the seed so that auto packing is deterministic.
    reproducibility.seed_all(0)

    # If max_seq_len is very small, skip profiling and select packing ratio of 1.
    dataset_config = dataloader_cfg['dataset']
    max_seq_len = dataset_config.get('max_seq_len')
    if max_seq_len <= 100:
        return 1

    min_ratio = 1
    if max_ratio is None:
        max_ratio = max_seq_len / 100
    elif max_ratio <= min_ratio:
        raise ValueError(f'{max_ratio=} must be >{min_ratio=}')
    assert isinstance(max_ratio, float)
    profiling_results = profile_packing(
        dataloader_cfg=dataloader_cfg,
        tokenizer=tokenizer,
        min_ratio=min_ratio,
        max_ratio=max_ratio,
        num_packing_ratios=num_packing_ratios,
        device_batch_size=device_batch_size,
        packing_collator_cls=packing_collator_cls,
        inner_collator=inner_collator,
    )

    # Obtain the maximum packing_ratio/minimum padding that has no waste.
    # profiling_results are sorted from smallest to largest packing_ratio.
    packing_ratio = 1
    for packing_ratio_candidate, _, waste in profiling_results:
        if waste is None or waste > 0:
            break
        packing_ratio = packing_ratio_candidate

    # Select the minimum packing ratio across all ranks.
    if dist.is_available() and dist.is_initialized():
        device = get_device(None)
        packing_ratio_tensor = device.tensor_to_device(
            torch.tensor(packing_ratio),
        )
        dist.all_reduce(packing_ratio_tensor, reduce_operation='MIN')
        packing_ratio = packing_ratio_tensor.item()

    # Restore rng state.
    reproducibility.load_rng_state(rng_state)

    return packing_ratio


def profile_packing(
    dataloader_cfg: dict[str, Any],
    tokenizer: PreTrainedTokenizerBase,
    min_ratio: float,
    max_ratio: float,
    num_packing_ratios: int,
    device_batch_size: int,
    packing_collator_cls: type[BinPackCollator] = BinPackCollator,
    inner_collator: Callable = lambda x: x,
) -> Iterable[tuple[float, Optional[float], Optional[float]]]:
    """Generator function that profiles example packing across packing ratios.

    Args:
        dataloader_cfg (DictConfig): The dataloader configuration for profiling.
        tokenizer (PreTrainedTokenizerBase): The tokenizer for profiling.
        min_ratio (float): Smallest packing_ratio to test. Must be >=1.
        max_ratio (float): Largest packing_ratio to test. Must be larger than `min_ratio`.
        num_packing_ratios (int): Number of packing_ratio values (spaced between `min_ratio` and `max_ratio`) to try.
        device_batch_size (int): The size of the batches (number of examples) per device.
        packing_collator_cls (BinPackCollator): The collator class to use for packing.
        inner_collator (Callable): The inner collator to use by the packing collator.

    Returns:
        An iterable of tuples of packing ratio, padding, and waste, sorted by smallest to largest packing ratio.
    """
    import copy

    from llmfoundry.data.dataloader import build_dataloader

    dataset_cfg = dataloader_cfg['dataset']
    max_seq_len = dataset_cfg.get('max_seq_len')
    max_leftovers_to_keep = dataset_cfg.get('max_leftovers_to_keep', None)

    # Turn off packing and sequence parallelism for the dataloader (we want raw, pre-packed, full-length examples)
    dataloader_cfg = copy.deepcopy(dataloader_cfg)
    dataloader_cfg.update({
        'drop_last': False,
        'num_workers': 0,
        'prefetch_factor': None,
        'persistent_workers': False,
    })
    dataset_cfg = dataloader_cfg['dataset']
    dataset_cfg['packing_ratio'] = 1.0
    seq_parallel_replication = dataset_cfg.get('seq_parallel_replication', 1)
    dataset_cfg['auto_packing_replication'] = seq_parallel_replication or 1
    dataset_cfg['seq_parallel_replication'] = 1
    dataset_cfg['pad_to_longest'] = True

    # If streaming dataset, use a temporary local folder for profiling
    local_rank_zero = dist.get_global_rank() - dist.get_local_rank()
    if dataset_cfg.get(
        'remote',
    ) is not None and dataset_cfg.get('local') is None:
        tmp_path_to_broadcast = tempfile.TemporaryDirectory().name
        gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
        tmp_path = gathered_paths[local_rank_zero]
        dataset_cfg['local'] = tmp_path

    if dataset_cfg.get('streams') is not None:
        for stream_config in dataset_cfg['streams'].values():
            tmp_path_to_broadcast = tempfile.TemporaryDirectory().name
            gathered_paths = dist.all_gather_object(tmp_path_to_broadcast)
            tmp_path = gathered_paths[local_rank_zero]
            if stream_config.get('local') is None:
                stream_config['local'] = tmp_path

    # Determine the packing_ratio values we'll try
    packing_ratios, raw_batch_sizes = [], []
    for packing_ratio in np.linspace(
        min_ratio,
        max_ratio,
        num_packing_ratios,
        endpoint=True,
    ):
        packing_ratio = np.round(10 * packing_ratio) / 10
        raw_batch_size = int(packing_ratio * device_batch_size)
        if raw_batch_size not in raw_batch_sizes:
            packing_ratios.append(packing_ratio)
            raw_batch_sizes.append(raw_batch_size)

    n_profile_examples = max(raw_batch_sizes) * 100

    train_dataspec = build_dataloader(
        dataloader_cfg,
        tokenizer,
        n_profile_examples,
    )
    train_dataloader = train_dataspec.dataloader

    # Get a bunch of raw examples
    big_batch = next(iter(train_dataloader))

    # Cut everything down to size
    if 'total_tokens' in big_batch:
        del big_batch['total_tokens']
    if 'loss_generating_tokens' in big_batch:
        del big_batch['loss_generating_tokens']

    sizes, trimmed_examples = packing_collator_cls._trim_batch(big_batch)

    def profile(raw_batch_size: int) -> tuple[Optional[float], Optional[float]]:
        # Copy trimmed examples so that the dicts are not shared between profiling runs.
        trimmed_examples_copy = [te.copy() for te in trimmed_examples]

        # Create the packing collator.
        packer = packing_collator_cls(
            collator=inner_collator,
            target_batch_size=device_batch_size,
            max_seq_len=max_seq_len,
            pad_token_id=0,  # <-- Doesn't need to be correct for profiling
            padding_side='left',  # <-- Doesn't need to be correct for profiling
            max_leftover_bins_to_keep=max_leftovers_to_keep,
            is_profiling=True,
        )

        # Simulate feeding the packing collator a bunch of data
        for idx in range(0, len(trimmed_examples_copy), raw_batch_size):
            batch = trimmed_examples_copy[idx:idx + raw_batch_size]
            if len(batch) < device_batch_size:
                continue
            packer._pack_trimmed_examples(
                batch,
                sizes[idx:idx + raw_batch_size],
            )

        if packer.n_packed_examples == 0:
            log.debug(
                'No examples packed during profiling. Dataset is smaller than device batch size.',
            )
            return None, None

        # Return the padding and waste stats over that bunch of data
        padding_percent = 100 * (1 - packer.efficiency)
        waste_percent = 100 * packer.waste
        return padding_percent, waste_percent

    log.debug('Profiling packing ratios')
    total_packing_ratios = min(len(packing_ratios), len(raw_batch_sizes))
    for i, (packing_ratio,
            raw_batch_size) in enumerate(zip(packing_ratios, raw_batch_sizes)):
        log.debug(
            f'Progress [{i}/{total_packing_ratios}]: Profiling packing ratio {packing_ratio}',
        )
        padding, waste = profile(raw_batch_size)
        yield (packing_ratio, padding, waste)
