import ctypes
import h5py
import json
import multiprocessing
import numpy as np
import pickle
import sys
import time

from enum import Enum
from hashlib import md5
from itertools import chain, product
from joblib import dump, load
from multiprocessing.sharedctypes import RawArray
from pathlib import Path
from sklearn.metrics import confusion_matrix, roc_auc_score
from sklearn.neighbors import KNeighborsClassifier
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from typing import List, Optional, Tuple, Type, Union

# Global variable for initialising pre-processing worker processes.
child_vars = {}


class KNNDataReader(object):
    """
    Data reader according to KNN baseline (as defined in class <KNeighborsClassifier>).
    """

    class Kernel(Enum):
        """
        Enumeration of supported kernels.
        """
        LINEAR = r'linear'
        JACCARD = r'jaccard'
        MIN_MAX = r'min_max'

        def __str__(self) -> str:
            return self.value

    # Class attributes for pre-processing data.
    spawn_method = r'spawn'
    tasks_per_child = None
    compression_algorithm = r'gzip'
    compression_shuffle = False
    compression_level = 4

    # Class attributes of <abundance> data type.
    hash_dtype = md5()
    count_dtype = np.long
    inner_dtype = np.dtype([(r'hash', r'S{}'.format(hash_dtype.digest_size * 2)), (r'count', count_dtype)])

    def __init__(self, file_path: Path, kernel: Kernel, indices: List[int] = None, load_metadata: bool = False,
                 dtype: Type = np.float32):
        """
        Initialise data reader according to KNN baseline (as defined in class <KNeighborsClassifier>).

        :param file_path: data file to read from (h5py)
        :param kernel: type of kernel to apply
        :param indices: indices of repertoires which are considered (all others are ignored)
        :param load_metadata: flag if metadata should be loaded (to extend the kernel)
        :param dtype: type of array to use
        """
        assert Path.exists(file_path) and h5py.is_hdf5(file_path), r'Invalid data file specified!'
        assert (indices is None) or all(_ >= 0 for _ in indices), r'Invalid repertoire indices specified!'

        self.__file_path = file_path
        self.__indices = indices
        self.__dtype = dtype

        with h5py.File(self.__file_path, r'r') as data_file:
            if indices is None:
                self.__size = len(data_file[r'metadata'][r'labels'][()])
                indices = range(self.__size)
            else:
                self.__size = len(indices)
                assert len(data_file[r'metadata'][r'labels'][()]) >= len(indices), r'Invalid <indices>!'
            try:
                similarity_key = kernel.name.lower()
            except AttributeError:
                raise ValueError(r'Invalid <kernel> specified! Aborting...')
            self.__kernel = 1.0 - data_file[r'sampledata'][f'{similarity_key}_similarity'][indices][:, indices]
            self.__target = np.maximum(0, data_file[r'metadata'][r'labels'][indices])

            # Optionally load abundance data to extend the kernel.
            if load_metadata:
                self.__kmer_size = data_file[r'metadata'][r'kmer_size'][()].item()
                self.__alphabet_size = data_file[r'metadata'][r'alphabet_size'][()].item()
                self.__kmer_presence = data_file[r'sampledata'][r'kmer_presence'][indices]
            else:
                self.__kmer_size = None
                self.__alphabet_size = None
                self.__kmer_presence = None

    def __len__(self) -> int:
        return self.__size

    def delete_metadata(self) -> None:
        """
        Delete metadata to free unnecessary memory.

        :return: None
        """
        del self.__kmer_size
        del self.__alphabet_size
        del self.__kmer_presence

    @classmethod
    def compute_similarities(cls, kmer_presence_buffer: Union[None, np.ndarray],
                             kmer_presence_shape: Union[None, Tuple[int, int]],
                             kmer_presence_mating_buffer: Union[None, np.ndarray],
                             kmer_presence_mating_shape: Union[None, Tuple[int, int]],
                             num_workers: int = 0, progress_bar: tqdm = None,
                             kernel: Union[None, Kernel] = None,
                             dtype: Type = np.float32) -> Tuple[Optional[np.ndarray],
                                                                Optional[np.ndarray], Optional[np.ndarray]]:
        """
        Compute similarity matrices on the basis of Jaccard and Min/Max kernels.

        :param kmer_presence_buffer: presence buffer of kmer-sequences in samples
        :param kmer_presence_shape: shape of <kmer_presence_buffer>
        :param kmer_presence_mating_buffer: presence buffer of kmer-sequences in mating samples
        :param kmer_presence_mating_shape: shape of <kmer_presence_mating_buffer>
        :param num_workers: amount of worker processes (data reading)
        :param progress_bar: progress bar to update after each similarity computation
        :param kernel: type of kernel to compute
        :param dtype: type of Tensor to use
        :return: computed similarity matrices
        """
        assert (kernel is None) or (type(kernel) == cls.Kernel), r'Invalid <kernel> specified! Aborting ...'
        progress_lock = multiprocessing.Lock()

        linear_similarity, jaccard_similarity, min_max_similarity = None, None, None
        size = kmer_presence_shape[0]
        size_mating = kmer_presence_mating_shape[0]
        jaccard_similarity = np.ones((size, size_mating), dtype=dtype)
        mating_equality = True if id(kmer_presence_buffer) == id(kmer_presence_mating_buffer) else False
        if (kernel is None) or (kernel == cls.Kernel.LINEAR):
            linear_similarity = np.ones((size, size_mating), dtype=dtype)
        if (kernel is None) or (kernel == cls.Kernel.JACCARD):
            jaccard_similarity = np.ones((size, size_mating), dtype=dtype)
        if (kernel is None) or (kernel == cls.Kernel.MIN_MAX):
            min_max_similarity = np.ones((size, size_mating), dtype=dtype)

        def _kmer_callback(kmer_result: Tuple[int, int, np.ndarray, np.ndarray, np.ndarray]) -> None:
            """
            Append result of similarity computation to overall statistics collection.

            :param kmer_result: result of similarity computation
            :return: None
            """
            _sample_index, _mating_index, _linear, _jaccard, _min_max = kmer_result
            if ((kernel is None) or (kernel == cls.Kernel.LINEAR)) and (len(_linear) > 0):
                linear_similarity[_sample_index, _mating_index:] = _linear
                if mating_equality:
                    linear_similarity[_mating_index:, _sample_index] = _linear
            if ((kernel is None) or (kernel == cls.Kernel.JACCARD)) and (len(_jaccard) > 0):
                jaccard_similarity[_sample_index, _mating_index:] = _jaccard
                if mating_equality:
                    jaccard_similarity[_mating_index:, _sample_index] = _jaccard
            if ((kernel is None) or (kernel == cls.Kernel.MIN_MAX)) and (len(_min_max) > 0):
                min_max_similarity[_sample_index, _mating_index:] = _min_max
                if mating_equality:
                    min_max_similarity[_mating_index:, _sample_index] = _min_max
            with progress_lock:
                if progress_bar is not None:
                    progress_bar.update(1)

        # Compute similarity matrices asynchronously.
        num_tasks = multiprocessing.cpu_count() if num_workers <= 0 else num_workers
        with multiprocessing.get_context(method=cls.spawn_method).Pool(
                processes=num_tasks, maxtasksperchild=cls.tasks_per_child, initializer=cls.init_child,
                initargs=(kmer_presence_buffer, kmer_presence_shape, kmer_presence_mating_buffer,
                          kmer_presence_mating_shape, None, None, None, None, None)) as sample_pool:

            sample_futures = []
            for sample_index in range(size - (1 if mating_equality else 0)):
                mating_index = (sample_index + 1) if mating_equality else 0
                sample_futures.append(sample_pool.apply_async(
                    cls.kmer_worker, (sample_index, mating_index, kernel, dtype),
                    callback=_kmer_callback,
                    error_callback=lambda _: print(f'Failed to process sample {sample_index + 1}!\n{_}\n')))

            # Wait for remaining tasks to be finished (remove futures to free any remaining allocated resources).
            while True:
                finished_futures = sorted([index for index, _ in enumerate(sample_futures) if _.ready()], reverse=True)
                for finished_future in finished_futures:
                    del sample_futures[finished_future]
                if len(sample_futures) <= 0:
                    break
                time.sleep(10)

        # Free unnecessary memory.
        del sample_futures

        return linear_similarity, jaccard_similarity, min_max_similarity

    @classmethod
    def adapt(cls, file_path: Path, store_path: Path, kmer_size: int = 4, num_workers: int = 0,
              dtype: Type = np.float32) -> None:
        """
        Adapt data set to be compatible with the KNN baseline (Jaccard and Min/Max kernels [1, 2]).

        [1] Levandowsky, M. and Winter, D. Distance between sets. Nature, 234(5323):34???35, 1971

        [2] Ralaivola, L., Swamidass, S. J., Saigo, H., and Baldi, P. Graph kernels for chemical informatics.
            Neural networks,18(8):1093???1110, 2005.

        :param file_path: data file to read from (h5py)
        :param store_path: path to resulting data file (h5py)
        :param kmer_size: size of a k-mer to extract
        :param num_workers: amount of worker processes (data reading)
        :param dtype: type of Tensor to use
        :return: None
        """
        with h5py.File(file_path, r'r') as data_file:
            size = data_file[r'metadata'][r'n_samples'][()].item()
            alphabet_size = len(data_file[r'metadata'][r'aas'][()])

        # Initialise shared memory.
        kmer_presence_shape = (size, alphabet_size ** kmer_size)
        kmer_presence_buffer = RawArray(ctypes.c_float, np.product(kmer_presence_shape).item())
        kmer_presence = np.frombuffer(kmer_presence_buffer, dtype=np.float32).reshape(kmer_presence_shape)
        kmer_presence.fill(0)

        # Compute statistics of data set.
        progress_bar_1 = tqdm(desc=r'[1/2] Compute sample statistics', unit=r'sa', total=size, file=sys.stdout)
        progress_bar_2 = tqdm(desc=r'[2/2] Compute custom kernels', unit=r'sa', total=size, file=sys.stdout)
        progress_lock = multiprocessing.Lock()

        def _sample_callback(_: None = None) -> None:
            """
            Update progress bar of sample analysis.

            :param _: None
            :return: None
            """
            with progress_lock:
                progress_bar_1.update(1)

        # Apply sample computations asynchronously.
        num_tasks = multiprocessing.cpu_count() if num_workers <= 0 else num_workers
        with multiprocessing.get_context(method=cls.spawn_method).Pool(
                processes=num_tasks, maxtasksperchild=cls.tasks_per_child, initializer=cls.init_child,
                initargs=(kmer_presence_buffer, kmer_presence_shape, None, None,
                          None, None, None, None, None)) as sample_pool:

            # Compute sample statistics.
            sample_futures = []
            with h5py.File(file_path, r'r') as data_file:
                if type(data_file['metadata'][r'labels']) == h5py.Group:
                    labels = np.where(data_file['metadata'][r'labels'][r'Known CMV status'])[1] * 2.0 - 1.0
                    labels[np.where(labels == 3.0)[0]] = 0.0
                else:
                    labels = np.where(data_file['metadata'][r'labels'])[1] * 2.0 - 1.0
                for sample_index in range(size):
                    start, end = data_file[r'sampledata'][r'sample_sequences_start_end'][sample_index]
                    sequence_lengths = data_file[r'sampledata'][r'seq_lens'][start:end]
                    sequence_counts = data_file[r'sampledata'][r'duplicates_per_sequence'][start:end]
                    sequences = data_file[r'sampledata'][r'amino_acid_sequences'][start:end]
                    sample_futures.append(sample_pool.apply_async(
                        cls.sample_worker,
                        (sample_index, sequence_lengths, sequence_counts, sequences, kmer_size, alphabet_size),
                        callback=_sample_callback,
                        error_callback=lambda _: print(f'Failed to process sample {sample_index + 1}!\n{_}\n')))

            # Wait for remaining tasks to be finished (remove futures to free any remaining allocated resources).
            while True:
                finished_futures = sorted([index for index, _ in enumerate(sample_futures) if _.ready()], reverse=True)
                for finished_future in finished_futures:
                    del sample_futures[finished_future]
                if len(sample_futures) <= 0:
                    break
                time.sleep(10)

        # Free unnecessary memory.
        del sample_futures

        # Compute similarity matrices.
        linear_similarity, jaccard_similarity, min_max_similarity = cls.compute_similarities(
            kmer_presence_buffer=kmer_presence_buffer, kmer_presence_shape=kmer_presence.shape,
            kmer_presence_mating_buffer=kmer_presence_buffer, kmer_presence_mating_shape=kmer_presence.shape,
            num_workers=num_workers, progress_bar=progress_bar_2, kernel=None, dtype=dtype)

        # Adapt data set and free unnecessary memory.
        with h5py.File(store_path, r'w') as data_file:

            # Write metadata (kmer-size, alphabet-size as well as target labels).
            data_file.require_dataset(
                r'metadata/kmer_size', shape=(1,), data=kmer_size, dtype=np.long)
            data_file.require_dataset(
                r'metadata/alphabet_size', shape=(1,), data=alphabet_size, dtype=np.long)
            data_file.require_dataset(
                r'metadata/labels', shape=labels.shape, data=labels, compression=cls.compression_algorithm,
                shuffle=cls.compression_shuffle, compression_opts=cls.compression_level, dtype=labels.dtype)
            data_file.flush()
            del labels

            # Write kmer-presence (to enable data set extension).
            data_file.require_dataset(
                r'sampledata/kmer_presence', shape=kmer_presence.shape, data=kmer_presence,
                compression=cls.compression_algorithm, shuffle=cls.compression_shuffle,
                compression_opts=cls.compression_level, dtype=kmer_presence.dtype)
            data_file.flush()
            del kmer_presence

            # Write linear similarity matrix.
            data_file.require_dataset(
                r'sampledata/linear_similarity', shape=linear_similarity.shape, data=linear_similarity,
                compression=cls.compression_algorithm, shuffle=cls.compression_shuffle,
                compression_opts=cls.compression_level, dtype=linear_similarity.dtype)
            data_file.flush()
            del linear_similarity

            # Write Jaccard similarity matrix.
            data_file.require_dataset(
                r'sampledata/jaccard_similarity', shape=jaccard_similarity.shape, data=jaccard_similarity,
                compression=cls.compression_algorithm, shuffle=cls.compression_shuffle,
                compression_opts=cls.compression_level, dtype=jaccard_similarity.dtype)
            data_file.flush()
            del jaccard_similarity

            # Write Min/Max similarity matrix.
            data_file.require_dataset(
                r'sampledata/min_max_similarity', shape=min_max_similarity.shape, data=min_max_similarity,
                compression=cls.compression_algorithm, shuffle=cls.compression_shuffle,
                compression_opts=cls.compression_level, dtype=min_max_similarity.dtype)
            data_file.flush()
            del min_max_similarity

    @classmethod
    def analyse(cls, file_path: Path, store_path: Path, kmer_size: int, num_workers: int = 0) -> None:
        """
        Analyse data set and construct sequence count matrix.

        :param file_path: data file to read from (h5py)
        :param store_path: path to resulting data file (h5py)
        :param kmer_size: size of a k-mer to extract
        :param num_workers: amount of worker processes (data reading)
        :return: None
        """
        with h5py.File(file_path, r'r') as data_file:
            size = data_file[r'metadata'][r'n_samples'][()].item()
            alphabet_size = len(data_file[r'metadata'][r'aas'][()])
            sequence_boundaries = data_file[r'sampledata'][r'n_sequences_per_sample'][()]
            num_sequences = sequence_boundaries.sum().item()
            sequence_boundaries = np.cumsum(np.concatenate(([0], sequence_boundaries)))

        # Initialise shared memory.
        abundance_buffer = RawArray(ctypes.c_byte, num_sequences * cls.inner_dtype.itemsize)
        abundance = np.frombuffer(abundance_buffer, dtype=cls.inner_dtype)

        # Compute statistics of data set.
        progress_bar_1 = tqdm(desc=r'[1/2] Compute sequence statistics', unit=r'sa', total=size, file=sys.stdout)
        progress_bar_2 = tqdm(desc=r'[2/2] Compute kmer statistics', unit=r'sa', total=size, file=sys.stdout)

        # Apply sample computations asynchronously.
        num_tasks = multiprocessing.cpu_count() if num_workers <= 0 else num_workers
        with multiprocessing.get_context(method=cls.spawn_method).Pool(
                processes=num_tasks, maxtasksperchild=cls.tasks_per_child, initializer=cls.init_child,
                initargs=(None, None, None, None, abundance_buffer, sequence_boundaries,
                          None, None, cls.inner_dtype)) as sample_pool:

            # Compute sample statistics.
            sample_futures = []
            with h5py.File(file_path, r'r') as data_file:
                for sample_index in range(size):
                    start, end = data_file[r'sampledata'][r'sample_sequences_start_end'][sample_index]
                    sequence_lengths = data_file[r'sampledata'][r'seq_lens'][start:end]
                    sequence_counts = data_file[r'sampledata'][r'duplicates_per_sequence'][start:end]
                    sequences = data_file[r'sampledata'][r'amino_acid_sequences'][start:end]
                    sample_futures.append(sample_pool.apply_async(
                        cls.sequence_worker, (
                            sample_index, sequence_lengths, sequence_counts, sequences, cls.inner_dtype),
                        callback=lambda _: progress_bar_1.update(1),
                        error_callback=lambda _: print(f'Failed to process sample {sample_index + 1}!\n{_}\n')))

            # Wait for remaining tasks to be finished (remove futures to free any remaining allocated resources).
            while True:
                finished_futures = sorted([index for index, _ in enumerate(sample_futures) if _.ready()], reverse=True)
                for finished_future in finished_futures:
                    del sample_futures[finished_future]
                if len(sample_futures) <= 0:
                    break
                time.sleep(10)

        # Free unnecessary memory.
        del sample_futures

        # Adapt data set and free unnecessary memory.
        with h5py.File(store_path, r'w') as data_file:

            # Write metadata (kmer and alphabet size, amount of sequences as well as dtype).
            inner_dtype = np.asarray([
                (k.encode(r'utf8'), str(v[0]).encode(r'utf8')) for k, v in dict(cls.inner_dtype.fields).items()])
            data_file.require_dataset(
                r'metadata/n_sequences', shape=(1,), data=num_sequences, dtype=cls.count_dtype)
            data_file.require_dataset(
                r'metadata/n_samples', shape=(1,), data=size, dtype=cls.count_dtype)
            data_file.require_dataset(
                r'metadata/kmer_size', shape=(1,), data=kmer_size, dtype=np.long)
            data_file.require_dataset(
                r'metadata/alphabet_size', shape=(1,), data=alphabet_size, dtype=np.long)
            data_file.require_dataset(
                r'metadata/dtype', shape=inner_dtype.shape, data=inner_dtype, dtype=inner_dtype.dtype)
            del inner_dtype

            # Write sequence abundance (to enable data set extension).
            data_file.require_dataset(
                r'sampledata/abundance_start_end', shape=sequence_boundaries.shape, data=sequence_boundaries,
                compression=cls.compression_algorithm, shuffle=cls.compression_shuffle,
                compression_opts=cls.compression_level, dtype=sequence_boundaries.dtype)
            data_file.require_dataset(
                r'sampledata/abundance', shape=abundance.shape, data=abundance,
                compression=cls.compression_algorithm, shuffle=cls.compression_shuffle,
                compression_opts=cls.compression_level, dtype=cls.inner_dtype.descr)
            data_file.flush()
            del sequence_boundaries
            del abundance

        # Initialise shared memory.
        kmer_presence_shape = (size, alphabet_size ** kmer_size)
        kmer_presence_buffer = RawArray(ctypes.c_long, np.product(kmer_presence_shape).item())
        kmer_presence = np.frombuffer(kmer_presence_buffer, dtype=cls.count_dtype).reshape(kmer_presence_shape)
        kmer_presence.fill(0)

        # Apply sample computations asynchronously.
        num_tasks = multiprocessing.cpu_count() if num_workers <= 0 else num_workers
        with multiprocessing.get_context(method=cls.spawn_method).Pool(
                processes=num_tasks, maxtasksperchild=cls.tasks_per_child, initializer=cls.init_child,
                initargs=(kmer_presence_buffer, kmer_presence_shape, None, None,
                          None, None, None, None, None)) as sample_pool:

            # Compute sample statistics.
            sample_futures = []
            with h5py.File(file_path, r'r') as data_file:
                for sample_index in range(size):
                    start, end = data_file[r'sampledata'][r'sample_sequences_start_end'][sample_index]
                    sequence_lengths = data_file[r'sampledata'][r'seq_lens'][start:end]
                    sequence_counts = data_file[r'sampledata'][r'duplicates_per_sequence'][start:end]
                    sequences = data_file[r'sampledata'][r'amino_acid_sequences'][start:end]
                    sample_futures.append(sample_pool.apply_async(
                        cls.sample_worker,
                        (sample_index, sequence_lengths, sequence_counts,
                         sequences, kmer_size, alphabet_size, False, cls.count_dtype),
                        callback=lambda _: progress_bar_2.update(1),
                        error_callback=lambda _: print(f'Failed to process sample {sample_index + 1}!\n{_}\n')))

            # Wait for remaining tasks to be finished (remove futures to free any remaining allocated resources).
            while True:
                finished_futures = sorted([index for index, _ in enumerate(sample_futures) if _.ready()], reverse=True)
                for finished_future in finished_futures:
                    del sample_futures[finished_future]
                if len(sample_futures) <= 0:
                    break
                time.sleep(10)

        # Free unnecessary memory.
        del sample_futures

        # Adapt data set and free unnecessary memory.
        with h5py.File(store_path, r'r+') as data_file:

            # Write kmer-presence (to enable data set extension).
            data_file.require_dataset(
                r'sampledata/kmer_presence', shape=kmer_presence.shape, data=kmer_presence,
                compression=cls.compression_algorithm, shuffle=cls.compression_shuffle,
                compression_opts=cls.compression_level, dtype=kmer_presence.dtype)
            data_file.flush()
            del kmer_presence

        # Close progress bars.
        progress_bar_2.close()
        progress_bar_1.close()

    @staticmethod
    def init_child(kmer_presence_buffer: Union[None, np.ndarray], kmer_presence_shape: Tuple[int, int],
                   kmer_presence_mating_buffer: Union[None, np.ndarray], kmer_presence_mating_shape: Tuple[int, int],
                   abundance_buffer: Union[None, np.ndarray], abundance_shape: List[int],
                   abundance_mating_buffer: Union[None, np.ndarray], abundance_mating_shape: List[int],
                   inner_dtype: np.dtype) -> None:
        """
        Initialise variables of pre-processing worker processes from global memory.

        :param kmer_presence_buffer: presence buffer of kmer-sequences in samples
        :param kmer_presence_shape: shape of <kmer_presence_buffer>
        :param kmer_presence_mating_buffer: presence buffer of kmer-sequences in mating samples
        :param kmer_presence_mating_shape: shape of <kmer_presence_mating_buffer>
        :param abundance_buffer: abundance buffer of sequences in samples
        :param abundance_shape: shape of <abundance_shape>
        :param abundance_mating_buffer: abundance buffer of sequences in mating samples
        :param abundance_mating_shape: shape of <abundance_mating_buffer>
        :param inner_dtype: type of <abundance> and <abundance_mating> samples
        :return: None
        """
        global child_vars

        child_vars[r'kmer_presence_buffer'] = kmer_presence_buffer
        child_vars[r'kmer_presence_shape'] = kmer_presence_shape
        child_vars[r'kmer_presence_mating_buffer'] = kmer_presence_mating_buffer
        child_vars[r'kmer_presence_mating_shape'] = kmer_presence_mating_shape
        child_vars[r'abundance_buffer'] = abundance_buffer
        child_vars[r'abundance_shape'] = abundance_shape
        child_vars[r'abundance_mating_buffer'] = abundance_mating_buffer
        child_vars[r'abundance_mating_shape'] = abundance_mating_shape
        child_vars[r'inner_dtype'] = inner_dtype

    @staticmethod
    def sample_worker(sample_index: int, sequence_lengths: np.ndarray, sequence_counts: np.ndarray,
                      sequences: np.ndarray, kmer_size: int, alphabet_size: int,
                      normalise: bool = True, count_dtype: np.dtype = np.float32) -> None:
        """
        Analyse sample with respect to presence of k-mers.

        :param sample_index: index of specific sample to analyse
        :param sequence_lengths: lengths of specified sequences to analyse
        :param sequence_counts: counts of specified sequences to analyse
        :param sequences: sequences to be analysed
        :param kmer_size: size of a k-mer to extract
        :param alphabet_size: number of different elements of the alphabet (amino acids)
        :param normalise: normalise kmer counts with respect to sequence counts
        :param count_dtype: data type of count array
        :return: sample index as well as k-mer presence statistics
        """
        keys = [r'_'.join(map(str, _)) for _ in product(range(alphabet_size), repeat=kmer_size)]
        sample_kmer_presence = np.zeros((len(keys),))
        keys = dict(zip(keys, range(len(keys))))

        # Initialise shared memory.
        kmer_presence = np.frombuffer(child_vars[r'kmer_presence_buffer'], dtype=count_dtype).reshape(
            child_vars[r'kmer_presence_shape'])

        # Compute presence of each <kmer> in the current sample.
        for sequence_index, (sequence_length, sequence_count, sequence) in enumerate(
                zip(sequence_lengths, sequence_counts, sequences)):
            trimmed_sequence = sequence[:sequence_length]
            for kmer_index in range(trimmed_sequence.shape[0] - kmer_size + 1):
                current_kmer = trimmed_sequence[kmer_index:kmer_index + kmer_size]
                sample_kmer_presence[keys[r'_'.join(current_kmer.astype(str).tolist())]] += 1 * sequence_count.item()

        # Store computed statistics in shared memory.
        total_sequence_count = sequence_counts.sum()
        if total_sequence_count > 0:
            kmer_presence[sample_index] = sample_kmer_presence.astype(dtype=count_dtype)
            if normalise:
                kmer_presence[sample_index] /= total_sequence_count
        else:
            kmer_presence[sample_index] = 0

    @staticmethod
    def sequence_worker(sample_index: int, sequence_lengths: np.ndarray, sequence_counts: np.ndarray,
                        sequences: np.ndarray, inner_dtype: np.dtype) -> None:
        """
        Analyse sample with respect to presence of sequences.

        :param sample_index: index of specific sample to analyse
        :param sequence_lengths: lengths of specified sequences to analyse
        :param sequence_counts: counts of specified sequences to analyse
        :param sequences: sequences to be analysed
        :param inner_dtype: type of <abundance> samples
        :return: sample index as well as k-mer presence statistics
        """

        # Initialise shared memory.
        start, end = child_vars[r'abundance_shape'][sample_index], child_vars[r'abundance_shape'][sample_index + 1]
        abundance = np.frombuffer(child_vars[r'abundance_buffer'], dtype=inner_dtype)[start:end]

        # Compute presence of each <kmer> in the current sample.
        for sequence_index, (sequence_length, sequence_count, sequence) in enumerate(
                zip(sequence_lengths, sequence_counts, sequences)):
            trimmed_sequence = sequence[:sequence_length]
            current_hash = md5(trimmed_sequence.data.tobytes()).hexdigest()
            current_count = sequence_count.item() if sequence_count.item() > 0 else 1
            abundance[sequence_index] = np.array((current_hash, current_count), dtype=inner_dtype)

    @staticmethod
    def kmer_worker(sample_index: int, mating_index: int, kernel: Union[None, Kernel],
                    dtype: Type = np.float32) -> Tuple[int, int, np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute similarities on the basis of kmer-sequences from specified sample.

        :param sample_index: index of specific sample to analyse
        :param mating_index: index of starting position of mating samples
        :param kernel: type of kernel to compute
        :param dtype: type of resulting kmer Tensor to use
        :return: sample index, mating sample index as well as computed similarities
        """
        global child_vars

        current_sample, current_sample_bool, current_sample_sum, current_sample_sum_bool = None, None, None, None
        linear_similarities, jaccard_similarities, min_max_similarities = np.zeros(
            (), dtype=dtype), np.zeros((), dtype=dtype), np.zeros((), dtype=dtype)

        # Prepare shared memory for all kernels.
        sample_kmer_presence = np.frombuffer(
            child_vars[r'kmer_presence_buffer'], dtype=np.float32).reshape(child_vars[r'kmer_presence_shape'])
        sample_kmer_presence_mating = np.frombuffer(
            child_vars[r'kmer_presence_mating_buffer'], dtype=np.float32).reshape(
            child_vars[r'kmer_presence_mating_shape'])
        num_samples = len(sample_kmer_presence_mating)

        # Prepare shared memory and auxiliary variables for linear similarity computation.
        if (kernel is None) or (kernel == KNNDataReader.Kernel.LINEAR):
            linear_similarities = np.zeros((sample_kmer_presence_mating.shape[0] - mating_index), dtype=dtype)

        # Prepare shared memory and auxiliary variables for Jaccard similarity computation.
        if (kernel is None) or (kernel == KNNDataReader.Kernel.JACCARD):
            current_sample_bool = sample_kmer_presence[sample_index].astype(np.bool)
            current_sample_sum_bool = current_sample_bool.sum()
            jaccard_similarities = np.zeros((sample_kmer_presence_mating.shape[0] - mating_index), dtype=dtype)

        # Prepare shared memory and auxiliary variables for Min/Max similarity computation.
        if (kernel is None) or (kernel == KNNDataReader.Kernel.MIN_MAX):
            current_sample = sample_kmer_presence[sample_index]
            min_max_similarities = np.zeros((sample_kmer_presence_mating.shape[0] - mating_index), dtype=dtype)

        # Compute similarities between repertoires.
        for inner_index, resulting_index in zip(range(mating_index, num_samples, 1), range(num_samples - mating_index)):

            # Compute linear similarity between repertoires.
            if (kernel is None) or (kernel == KNNDataReader.Kernel.LINEAR):
                linear_similarities[resulting_index] = dtype(
                    np.dot(a=sample_kmer_presence_mating[inner_index], b=current_sample))

            # Compute Jaccard similarity between repertoires.
            if (kernel is None) or (kernel == KNNDataReader.Kernel.JACCARD):
                num_intersecting = sample_kmer_presence_mating[inner_index].astype(np.bool) * current_sample_bool
                num_intersecting = num_intersecting.sum()
                num_union = sample_kmer_presence_mating[inner_index].astype(np.bool).sum() + current_sample_sum_bool
                num_union -= num_intersecting
                if num_union != 0:
                    jaccard_similarities[resulting_index] = dtype(
                        num_intersecting.astype(dtype=np.float64) / num_union.astype(dtype=np.float64))

            # Compute Min/Max similarity between repertoires.
            if (kernel is None) or (kernel == KNNDataReader.Kernel.MIN_MAX):
                stacked_presence = np.stack((sample_kmer_presence_mating[inner_index], current_sample))
                num_union = np.max(stacked_presence, axis=0).sum()
                if num_union != 0:
                    min_max_similarities[resulting_index] = dtype(np.min(stacked_presence, axis=0).sum() / num_union)

        return sample_index, mating_index, linear_similarities, jaccard_similarities, min_max_similarities

    @property
    def kernel(self) -> np.ndarray:
        return self.__kernel

    @property
    def target(self) -> np.ndarray:
        return self.__target

    @property
    def kmer_presence(self) -> Union[None, np.ndarray]:
        return self.__kmer_presence

    @property
    def kmer_size(self) -> Union[None, int]:
        return self.__kmer_size

    @property
    def alphabet_size(self) -> Union[None, int]:
        return self.__alphabet_size


class KNNBaseline(object):
    """
    Supervisory instance for operating KNN baseline (as defined in class <KNeighborsClassifier>).
    """

    def __init__(self, file_path: Path, kernel: KNNDataReader.Kernel, fold_info: Union[None, int, Path] = 5,
                 load_metadata: bool = False, dtype: Type = np.float32, test_mode: bool = False, offset: int = 0):
        """
        Initialise supervisory instance for operating KNN baseline (as defined in class <KNeighborsClassifier>).

        :param file_path: data file to read from (h5py)
        :param kernel: type of kernel to apply
        :param fold_info: number of folds for cross-validation (or <None> to use the whole data set)
        :param load_metadata: flag if metadata should be loaded (to extend the kernel)
        :param dtype: type of array to use
        :param test_mode: flag if KNN baseline should be loaded in test mode
        :param offset: offset used to define evaluation, test, and training folds
        """
        assert Path.exists(file_path) and h5py.is_hdf5(file_path), r'Invalid data file specified!'
        assert (fold_info is None) or ((type(fold_info) == int) and (fold_info > 1)) or Path.is_file(fold_info)

        self.__kernel = kernel
        self.__fold_info = fold_info
        self.__num_folds = None
        self.__indices_test = None
        self.__indices_test_resort = None
        self.__dtype = dtype

        self.__data_reader = KNNDataReader(file_path=file_path, kernel=kernel, indices=None,
                                           load_metadata=load_metadata, dtype=self.__dtype)
        if type(fold_info) == int:
            self.__num_folds = fold_info
            with h5py.File(file_path, r'r') as data_file:
                num_repertoires = len(data_file[r'metadata'][r'labels'][()])
                assert num_repertoires >= self.__num_folds
                indices = np.arange(num_repertoires, dtype=np.long)
                np.random.shuffle(indices)
                indices = np.split(indices, indices_or_sections=self.__num_folds, axis=0)
                self.__indices = [list(np.sort(fold_indices, axis=0)) for fold_indices in indices]
                self.__indices_resort = [list(np.argsort(fold_indices, axis=0)) for fold_indices in indices]
        elif fold_info is not None:
            with open(self.__fold_info, r'br') as pickle_file:
                self.__fold_info = pickle.load(pickle_file)
                indices_folds = self.__fold_info[r'inds']
                eval_offset = min(offset, len(indices_folds) - 1)
                test_offset = 0 if ((eval_offset + 1) >= len(indices_folds)) else (eval_offset + 1)
                indices_train = np.concatenate([
                    fold for _, fold in enumerate(indices_folds) if all([_ != eval_offset, _ != test_offset])], axis=0)
                indices_eval = indices_folds[eval_offset]
                self.__indices_test = np.sort(indices_folds[test_offset], axis=0) if test_mode else None
                self.__indices_test_resort = np.argsort(indices_folds[test_offset], axis=0) if test_mode else None
            self.__num_folds = 1
            self.__indices = [
                list(np.sort(fold_indices, axis=0)) for fold_indices in [indices_eval, indices_train]]
            self.__indices_resort = [
                list(np.argsort(fold_indices, axis=0)) for fold_indices in [indices_eval, indices_train]]

    def optimise(self, num_neighbours: Tuple[int, int], seed: int = 42, log_dir: Path = None):
        """
        Optimise hyperparameters of KNN baseline according to balanced accuracy.

        :param num_neighbours: range of the amount of neighbours parameter <n_neighbors> of the KNN
        :param seed: seed to be used for reproducibility
        :param log_dir: directory to store TensorBoard logs
        :return: best hyperparameters found by cross-validation
        """
        log_writer = SummaryWriter(log_dir=str(log_dir)) if log_dir is not None else None
        best_hyperparameters = {r'neighbours': float()}
        best_performance = -np.inf

        num_samples = np.inf
        for fold in range(self.__num_folds):
            num_samples = np.minimum(num_samples, len(list(chain.from_iterable(
                [indices for _, indices in enumerate(self.__indices) if _ != fold]))))

        # Draw hyperparameter values of the trial.
        np.random.seed(seed)
        num_neighbours = list(range(min(num_neighbours), min(max(num_neighbours), int(num_samples)) + 1, 1))

        progress_bar_1 = tqdm(total=len(num_neighbours), desc=r'Trial', unit=r'tr', position=0, file=sys.stdout)
        progress_bar_2 = tqdm(total=self.__num_folds, desc=r'Fold', unit=r'fo', position=1, file=sys.stdout)

        # Perform grid search to optimise hyperparameters.
        for trial in range(len(num_neighbours)):
            np.random.seed(seed)

            # Save current hyperparameters.
            if log_dir is not None:
                with open(str((log_dir / Path(f'trial_{trial + 1}_hyperparameters.json'))),
                          r'w') as hyperparameters_json:
                    json.dump({r'neighbours': num_neighbours[trial]}, hyperparameters_json)

            # Create KNN baseline modules.
            knn_modules = [KNeighborsClassifier(
                n_neighbors=num_neighbours[trial], metric=r'precomputed') for _ in range(self.__num_folds)]

            # Train KNN baseline modules.
            fold_confusion_matrix = {
                r'train': np.zeros((2, 2), dtype=self.__dtype), r'eval': np.zeros((2, 2), dtype=self.__dtype)}
            fold_roc_auc = {
                r'train': np.zeros((1,), dtype=self.__dtype), r'eval': np.zeros((1,), dtype=self.__dtype)}
            for fold in range(len(knn_modules)):
                ignore_index = fold if self.__num_folds > 1 else 0
                fold_indices_fit = list(
                    chain.from_iterable([indices for _, indices in enumerate(self.__indices) if _ != ignore_index]))
                knn_modules[fold].fit(X=self.__data_reader.kernel[fold_indices_fit][:, fold_indices_fit],
                                      y=self.__data_reader.target[fold_indices_fit])

                # Evaluate model on training fold.
                fold_predictions = knn_modules[fold].predict(
                    X=self.__data_reader.kernel[fold_indices_fit][:, fold_indices_fit])
                fold_probabilities = np.max(knn_modules[fold].predict_proba(
                    X=self.__data_reader.kernel[fold_indices_fit][:, fold_indices_fit]), axis=1)
                fold_confusion_matrix[r'train'] += confusion_matrix(
                    y_true=self.__data_reader.target[fold_indices_fit], y_pred=fold_predictions, labels=[0, 1])
                fold_roc_auc[r'train'] += roc_auc_score(
                    y_true=self.__data_reader.target[fold_indices_fit], y_score=fold_probabilities)

                # Evaluate model on evaluation fold.
                fold_predictions = knn_modules[fold].predict(
                    X=self.__data_reader.kernel[self.__indices[fold]][:, fold_indices_fit])
                fold_probabilities = np.max(knn_modules[fold].predict_proba(
                    X=self.__data_reader.kernel[self.__indices[fold]][:, fold_indices_fit]), axis=1)
                fold_confusion_matrix[r'eval'] += confusion_matrix(
                    y_true=self.__data_reader.target[self.__indices[fold]], y_pred=fold_predictions, labels=[0, 1])
                fold_roc_auc[r'eval'] += roc_auc_score(
                    y_true=self.__data_reader.target[self.__indices[fold]], y_score=fold_probabilities)
                progress_bar_2.update(1)

            # Evaluate current model with respect to the training data.
            tn, fp, fn, tp = fold_confusion_matrix[r'train'].flatten()
            sensitivity = {r'train': np.nan_to_num(tp / (tp + fn), nan=1.0)}
            specificity = {r'train': np.nan_to_num(tn / (tn + fp), nan=1.0)}
            balanced_accuracy = {r'train': np.nan_to_num((sensitivity[r'train'] + specificity[r'train']) / 2.0)}
            fold_roc_auc[r'train'] /= self.__num_folds

            # Evaluate current model and compare with current best performing one.
            tn, fp, fn, tp = fold_confusion_matrix[r'eval'].flatten()
            sensitivity[r'eval'] = np.nan_to_num(tp / (tp + fn), nan=1.0)
            specificity[r'eval'] = np.nan_to_num(tn / (tn + fp), nan=1.0)
            balanced_accuracy[r'eval'] = np.nan_to_num((sensitivity[r'eval'] + specificity[r'eval']) / 2.0)
            fold_roc_auc[r'eval'] /= self.__num_folds
            if best_performance < fold_roc_auc[r'eval']:
                best_performance = fold_roc_auc[r'eval']
                best_hyperparameters[r'neighbours'] = num_neighbours[trial]

            if log_dir is not None:
                log_writer.add_scalar(
                    tag=r'balanced_accuracy/train', scalar_value=balanced_accuracy[r'train'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'sensitivity/train', scalar_value=sensitivity[r'train'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'specificity/train', scalar_value=specificity[r'train'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'roc_auc/train', scalar_value=fold_roc_auc[r'train'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'balanced_accuracy/eval', scalar_value=balanced_accuracy[r'eval'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'sensitivity/eval', scalar_value=sensitivity[r'eval'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'specificity/eval', scalar_value=specificity[r'eval'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'roc_auc/eval', scalar_value=fold_roc_auc[r'eval'], global_step=trial + 1)
                log_writer.add_scalar(
                    tag=r'num_neighbours', scalar_value=num_neighbours[trial], global_step=trial + 1)
            progress_bar_2.reset()
            progress_bar_1.update(1)
        progress_bar_1.refresh()

        # Save best KNN baseline module.
        if log_dir is not None and self.__num_folds == 1:
            # Re-fit KNN module according to best hyperparameters.
            best_knn_module = KNeighborsClassifier(
                n_neighbors=best_hyperparameters[r'neighbours'], metric=r'precomputed')
            fold_indices_fit = list(
                chain.from_iterable([indices for _, indices in enumerate(self.__indices) if _ != 0]))
            best_knn_module.fit(X=self.__data_reader.kernel[fold_indices_fit][:, fold_indices_fit],
                                y=self.__data_reader.target[fold_indices_fit])

            # Save re-fitted KNN module.
            best_knn_module.__dict__[r'kmer_size'] = int(self.__data_reader.kmer_size)
            best_knn_module.__dict__[r'alphabet_size'] = int(self.__data_reader.alphabet_size)
            best_knn_module.__dict__[r'kernel_type'] = str(self.__kernel)
            best_knn_module.__dict__[r'kmer_presence'] = self.__data_reader.kmer_presence[fold_indices_fit]
            dump(value=best_knn_module, filename=str(log_dir / r'final_model.knn'))

        # CLose summary writer.
        if log_dir is not None:
            log_writer.close()

        # Close progress bars.
        progress_bar_2.close()
        progress_bar_1.close()

        return best_hyperparameters

    def train(self, file_path_output: Path, num_neighbours: float, seed: int = 42) -> None:
        """
        Train KNN baseline module and save resulting model to disk.

        :param file_path_output: path to store resulting model
        :param num_neighbours: range of the amount of neighbours parameter <n_neighbors> of the KNN
        :param seed: seed to be used for reproducibility
        :return: None
        """
        np.random.seed(seed)

        # Create and fit KNN baseline module.
        knn_module = KNeighborsClassifier(n_neighbors=num_neighbours, metric=r'precomputed')
        knn_module.fit(X=self.__data_reader.kernel, y=self.__data_reader.target)

        # Save fitted KNN baseline module.
        knn_module.__dict__[r'kmer_size'] = int(self.__data_reader.kmer_size)
        knn_module.__dict__[r'alphabet_size'] = int(self.__data_reader.alphabet_size)
        knn_module.__dict__[r'kernel_type'] = str(self.__kernel)
        knn_module.__dict__[r'kmer_presence'] = self.__data_reader.kmer_presence
        dump(value=knn_module, filename=str(file_path_output))

    def predict(self, knn_module: KNeighborsClassifier, activations: bool = False,
                num_workers: int = 0) -> Tuple[List[Union[int, float]], Optional[float]]:
        """
        Predict per-repertoire label activations according to KNN baseline.

        :param knn_module: pre-trained <KNeighborsClassifier> instance
        :param activations: return activations instead of discrete predictions
        :param num_workers: amount of worker processes (data reading)
        :return: per-repertoire label activations
        """
        if self.__indices_test is None:

            # Predict per-repertoire label activations on specified data set.
            kernel_type = KNNDataReader.Kernel[knn_module.__dict__[r'kernel_type'].strip().upper()]
            progress_bar = tqdm(desc=r'Extend kernel', unit=r'sa', total=len(self.__data_reader), file=sys.stdout)

            # Prepare shared memory and auxiliary variables for Jaccard/Min-Max similarity computation.
            kmer_presence_shape = self.__data_reader.kmer_presence.shape
            kmer_presence_buffer = RawArray(ctypes.c_float, np.product(kmer_presence_shape).item())
            kmer_presence = np.frombuffer(kmer_presence_buffer, dtype=np.float32).reshape(kmer_presence_shape)
            np.copyto(kmer_presence, self.__data_reader.kmer_presence)

            kmer_presence_mating_shape = knn_module.__dict__[r'kmer_presence'].shape
            kmer_presence_mating_buffer = RawArray(ctypes.c_float, np.product(kmer_presence_mating_shape).item())
            kmer_presence_mating = np.frombuffer(kmer_presence_mating_buffer, dtype=np.float32).reshape(
                kmer_presence_mating_shape)
            np.copyto(kmer_presence_mating, knn_module.__dict__[r'kmer_presence'])

            # Compute Jaccard/Min-Max similarities between repertoires.
            linear_similarity, jaccard_similarity, min_max_similarity = self.__data_reader.compute_similarities(
                kmer_presence_buffer=kmer_presence_buffer,
                kmer_presence_shape=kmer_presence_shape, kmer_presence_mating_buffer=kmer_presence_mating_buffer,
                kmer_presence_mating_shape=kmer_presence_mating_shape, num_workers=num_workers,
                progress_bar=progress_bar, kernel=kernel_type, dtype=self.__dtype)

            # Free unnecessary memory.
            self.__data_reader.delete_metadata()
            del knn_module.__dict__[r'kmer_presence']

            if kernel_type == self.__data_reader.Kernel.LINEAR:
                kernel_matrix = linear_similarity
            elif kernel_type == self.__data_reader.Kernel.JACCARD:
                kernel_matrix = jaccard_similarity
            else:
                kernel_matrix = min_max_similarity

            if activations:
                result = np.max(knn_module.predict_proba(X=kernel_matrix), axis=1).tolist()
            else:
                result = np.maximum(0, knn_module.predict(X=kernel_matrix).astype(np.int32)).tolist()

            return result, None

        else:

            # Evaluate model on test fold.
            fold_indices_fit = list(
                chain.from_iterable([indices for _, indices in enumerate(self.__indices) if _ != 0]))
            kernel_matrix = self.__data_reader.kernel[self.__indices_test][:, fold_indices_fit]

            result = np.max(knn_module.predict_proba(X=kernel_matrix), axis=1)
            roc_auc = roc_auc_score(y_true=self.__data_reader.target[self.__indices_test], y_score=result)
            if not activations:
                result = np.maximum(0, knn_module.predict(X=kernel_matrix).astype(np.int32))

            result_resorted = np.zeros_like(result)
            result_resorted[self.__indices_test_resort] = result
            return list(result_resorted), roc_auc

    def predict_from_path(self, file_path_model: Path, activations: bool = False,
                          num_workers: int = 0) -> Tuple[List[Union[int, float]], Optional[float]]:
        """
        Predict per-repertoire label activations according to KNN baseline.

        :param file_path_model: path to pre-trained <KNeighborsClassifier> instance
        :param activations: return activations instead of discrete predictions
        :param num_workers: amount of worker processes (data reading)
        :return: per-repertoire label activations
        """
        knn_module = load(filename=str(file_path_model))
        return self.predict(knn_module=knn_module, activations=activations, num_workers=num_workers)


def main():
    # Initialise argument parsers for ...
    arg_parser = argparse.ArgumentParser(
        description=r'KNN baseline model for disease status prediction')
    arg_sub_parsers = arg_parser.add_subparsers(
        dest=r'mode', required=True)
    # ... data set adaption.
    adapt_parser = arg_sub_parsers.add_parser(
        name=r'adapt', help=r'adapt data set to be compatible with KNN baseline')
    adapt_parser.add_argument(
        r'-i', r'--input', type=str, help=r'data file (h5py) to use', required=True)
    adapt_parser.add_argument(
        r'-o', r'--output', type=str, help=r'path to resulting data file (h5py)', required=True)
    adapt_parser.add_argument(
        r'-z', r'--kmer_size', type=int, help=r'size <k> of a k-mer', default=4)
    adapt_parser.add_argument(
        r'-w', r'--worker', type=int, help=r'number of worker proc. (data reading)', default=-1)
    # ... hyperparameter optimisation.
    hyper_parser = arg_sub_parsers.add_parser(
        name=r'optim', help=r'perform hyperparameter optimisation')
    hyper_parser.add_argument(
        r'-i', r'--input', type=str, help=r'data file (h5py) to use', required=True)
    hyper_parser.add_argument(
        r'-o', r'--output', type=str, help=r'path to store best hyperparameters', required=True)
    hyper_parser.add_argument(
        r'-g', r'--log_dir', type=str, help=r'directory to store TensorBoard logs', default=None)
    hyper_parser.add_argument(
        r'-k', r'--kernel', type=KNNDataReader.Kernel, help=r'type of kernel', required=True)
    hyper_parser_folds = hyper_parser.add_mutually_exclusive_group(required=False)
    hyper_parser_folds.add_argument(
        r'-f', r'--folds', type=int, help=r'number of folds')
    hyper_parser_pickle = hyper_parser_folds.add_argument_group()
    hyper_parser_pickle.add_argument(
        r'-z', r'--pickle', type=str, help=r'fold definitions (pickle-file)')
    hyper_parser_pickle.add_argument(
        r'-l', r'--offset', type=int, help=r'offset defining the folds for training/evaluation/test splits', default=0)
    hyper_parser.add_argument(
        r'-n', r'--neighbours', type=int, help=r'range of neighbours parameter of the KNN', nargs=2, required=True)
    hyper_parser.add_argument(
        r'-s', r'--seed', type=int, help=r'seed to be used for reproducibility', default=42)
    # ... training.
    train_parser = arg_sub_parsers.add_parser(
        name=r'train', help=r'train KNN baseline model')
    train_parser.add_argument(
        r'-i', r'--input', type=str, help=r'data set (h5py) to use', required=True)
    train_parser.add_argument(
        r'-o', r'--output', type=str, help=r'path to store resulting model', required=True)
    train_parser.add_argument(
        r'-k', r'--kernel', type=KNNDataReader.Kernel, help=r'type of kernel', required=True)
    train_parser.add_argument(
        r'-s', r'--seed', type=int, help=r'seed to be used for reproducibility', default=42)
    train_parser_main_group = train_parser.add_mutually_exclusive_group(required=True)
    train_parser_main_group.add_argument(
        r'-j', r'--json', type=str, help=r'hyperparameters to use (json)')
    train_parser_main_group.add_argument(
        r'-n', r'--neighbours', type=int, help=r'neighbours parameter of the KNN')
    # ...prediction.
    predict_parser = arg_sub_parsers.add_parser(
        name=r'predict', help=r'predict disease status using pre-trained model')
    predict_parser.add_argument(
        r'-i', r'--input', type=str, help=r'data set (h5py) to use', required=True)
    predict_parser.add_argument(
        r'-o', r'--output_dir', type=str, help=r'directory to store predictions (and ROC AUC)', default=None)
    predict_parser.add_argument(
        r'-a', r'--activations', action=r'store_true', help=r'compute activations instead of discrete predictions')
    predict_parser.add_argument(
        r'-m', r'--model', type=str, help=r'model to be used for prediction', required=True)
    predict_parser.add_argument(
        r'-z', r'--pickle', type=str, help=r'fold definitions (pickle-file)', default=None)
    predict_parser.add_argument(
        r'-w', r'--worker', type=int, help=r'number of worker proc. (data reading)', default=-1)
    predict_parser.add_argument(
        r'-l', r'--offset', type=int, help=r'offset defining the folds for training/evaluation/test splits', default=0)
    # Parse arguments.
    args = arg_parser.parse_args()

    # Execute KNN baseline.
    if args.mode == r'adapt':

        # Adapt data set (compute auxiliary features).
        KNNDataReader.adapt(file_path=Path(args.input), store_path=Path(args.output), kmer_size=args.kmer_size,
                            num_workers=args.worker, dtype=np.float32)

    elif args.mode == r'optim':

        # Create and optimise KNN baseline.
        knn_baseline = KNNBaseline(file_path=Path(args.input), kernel=args.kernel,
                                   fold_info=args.folds if (args.pickle is None) else Path(args.pickle),
                                   load_metadata=True, dtype=np.float32, test_mode=False, offset=args.offset)
        hyperparameters = knn_baseline.optimise(num_neighbours=args.neighbours, seed=args.seed,
                                                log_dir=None if args.log_dir is None else Path(args.log_dir))

        # Store best hyperparameters as obtained by grid search.
        output_directory = os.path.dirname(args.output)
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)
        with open(args.output, r'w') as hyperparameters_json:
            json.dump(hyperparameters, hyperparameters_json)

    elif args.mode == r'train':

        # Process data file according to KNN baseline.
        knn_baseline = KNNBaseline(
            file_path=Path(args.input), kernel=args.kernel, fold_info=None,
            load_metadata=True, dtype=np.float32, test_mode=False)

        # Fetch hyperparameters to be used.
        hyperparameters = None
        if args.json is not None:
            with open(args.json, r'r') as hyperparameters_json:
                hyperparameters = json.load(hyperparameters_json)
        else:
            hyperparameters = {r'neighbours': args.neighbours}

        # Train KNN baseline.
        knn_baseline.train(
            file_path_output=Path(args.output), num_neighbours=args.neighbours, seed=args.seed)

    elif args.mode == r'predict':

        # Check output directory.
        output_result, output_roc_auc = None, None
        output_directory = None if ((args.output_dir is None) or (len(args.output_dir) == 0)) else Path(args.output_dir)
        if output_directory is not None:
            assert output_directory.exists() and output_directory.is_dir(), r'Invalid data file specified!'
            output_result = open(str(output_directory / r'predictions.txt'), mode=r'w')
            if args.pickle is not None:
                output_roc_auc = open(str(output_directory / r'roc_auc.txt'), mode=r'w')

        # Fetch auxiliary information from trained KNN baseline model.
        knn_module = load(filename=args.model)
        kernel_type = KNNDataReader.Kernel[knn_module.__dict__[r'kernel_type'].strip().upper()]
        del knn_module

        # Predict using pre-trained KNN baseline model.
        knn_baseline = KNNBaseline(
            file_path=Path(args.input), kernel=kernel_type,
            fold_info=None if (args.pickle is None) else Path(args.pickle),
            load_metadata=True, dtype=np.float32, test_mode=True, offset=args.offset)
        result = knn_baseline.predict_from_path(
            file_path_model=Path(args.model), activations=args.activations, num_workers=args.worker)

        # Print (or store) results.
        if output_result is None:
            print(f'[ROC AUC]\n{result[1]}', end='\n')
        elif result[1] is not None:
            print(result[1], end='\n', file=output_roc_auc)
        if output_directory is None:
            print(r'[PREDICTIONS]', end='\n')
        for prediction in result[0]:
            print(prediction, end='\n', file=output_result)

    else:
        raise ValueError(r'Invalid <mode> specified! Aborting...')
