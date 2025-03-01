    # Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from collections import Counter
import os
from onmt.utils import safe_readline, safe_readaudio
# from multiprocessing import Pool
import torch.multiprocessing as mp
import torch
import onmt
import numpy as np
from .audio_utils import ArkLoader


class SpeechBinarizer:

    def __init__(self):
        pass

    @staticmethod
    def binarize_h5_file(filename, output_format='raw',
                         prev_context=0, concat=4, stride=1, fp16=False):

        file_idx = -1;
        if filename[-2:] == "h5":
            srcf = h5.File(filename, 'r')
        else:
            file_idx = 0
            srcf = h5.File(filename + "." + str(file_idx) + ".h5", 'r')

        while True:
            if input_format == "h5":
                if str(index) in srcf:
                    feature_vector = np.array(srcf[str(index)])
                elif file_idx != -1:
                    srcf.close()
                    file_idx += 1
                    srcf = h5.File(src_file + "." + str(file_idx) + ".h5", 'r')
                    feature_vector = np.array(srcf[str(index)])
                else:
                    print("No feature vector for index:", index, file=sys.stderr)
                    break

        raise NotImplementedError

    @staticmethod
    def binarize_file_single_thread(filename, ark_loader, offset=0, end=-1, worker_id=0,
                                    input_format='scp', output_format='raw',
                                    prev_context=0, concat=4, stride=1, fp16=False, sample_rate=16000):
        # if output_format is scp, we only read the length for sorting

        if output_format == 'scp':
            assert input_format in ['kaldi', 'scp']
        if output_format == 'wav':
            input_format = 'wav'

        # audio_data = iter(ReadHelper('scp:' + filename))
        # data_file = open(filename)
        # data_keys = list(data.keys())
        # data_paths = list(data._dict.values())

        result = dict()
        data = list()
        lengths = list()
        index = 0

        with open(filename, 'r', encoding='utf-8') as f:
            f.seek(offset)

            line = safe_readline(f)

            while line:
                if 0 < end < f.tell():
                    break

                parts = line.split()

                key = parts[0]

                # this special case is for the "preceeding"
                if key == 'NULL':
                    feature_vector = torch.zeros(0, 0)
                    lengths.append(feature_vector.size(0))
                    line = f.readline()
                    continue

                if input_format in ['scp', 'kaldi']:
                    # an scp file has the format: uttid path:mem
                    path = parts[1]
                    # read numpy array from the ark here
                    feature_vector = ark_loader.load_mat(path)

                    if stride == 1:
                        feature_vector = torch.from_numpy(feature_vector)
                    else:
                        feature_vector = torch.from_numpy(feature_vector[0::opt.stride])

                    if concat > 1:
                        add = (concat - feature_vector.size()[0] % concat) % concat
                        z = torch.FloatTensor(add, feature_vector.size()[1]).zero_()
                        feature_vector = torch.cat((feature_vector, z), 0)
                        feature_vector = feature_vector.reshape((int(feature_vector.size()[0] / concat),
                                                                 feature_vector.size()[1] * concat))

                    if prev_context > 0:
                        print("Multiple ASR context isn't supported at the moment   ")
                        raise NotImplementedError

                    if fp16 and output_format not in ['scp', 'scpmem']:
                        feature_vector = feature_vector.half()

                    if output_format not in ['scp', 'scpmem']:
                        data.append(feature_vector.numpy())  # convert to numpy for serialization
                    else:
                        data.append(path)

                elif input_format == 'wav':

                    # an wav input file should have format uttid wav_file start end
                    # in which the start and end (by second) can be 0 0
                    if len(parts) >= 4:
                        wavpath, start_time, end_time = parts[1], float(parts[2]), float(parts[3])
                    else:
                        wavpath = parts[1]
                        start_time = 0
                        end_time = -1
                    feature_vector = safe_readaudio(wavpath, start_time, end_time, sample_rate=sample_rate)
                    # store a tuple of data and information to load the wav again during training
                    data.append((wavpath, start_time, end_time, sample_rate))

                lengths.append(feature_vector.size(0))

                line = f.readline()

                if (index + 1) % 100000 == 0:
                    print("[INFO] Thread %d Processed %d audio utterances." % (worker_id, index + 1))

                index = index + 1

        result['data'] = data
        result['sizes'] = lengths
        result['id'] = worker_id
        result['total'] = len(lengths)

        return result

    @staticmethod
    def binarize_file(filename, input_format='scp', output_format='raw',
                      prev_context=0, concat=4, stride=1, fp16=False, num_workers=1):

        if input_format == 'h5':
            return SpeechBinarizer.binarize_h5_file(filename, output_format, prev_context, concat,
                                                    stride, fp16)

        result = dict()

        for i in range(num_workers):
            result[i] = dict()

        final_result = dict()

        def merge_result(bin_result):
            result[bin_result['id']]['data'] = bin_result['data']
            result[bin_result['id']]['sizes'] = bin_result['sizes']

        offsets = Binarizer.find_offsets(filename, num_workers)

        ark_loaders = dict()
        for i in range(num_workers):
            ark_loaders[i] = ArkLoader()

        if num_workers > 1:

            pool = mp.Pool(processes=num_workers)
            mp_results = []

            for worker_id in range(num_workers):
                mp_results.append(pool.apply_async(
                    SpeechBinarizer.binarize_file_single_thread,
                    args=(filename, ark_loaders[worker_id], offsets[worker_id], offsets[worker_id + 1], worker_id,
                          input_format, output_format, prev_context, concat, stride, fp16),
                ))

            pool.close()
            pool.join()

            for r in mp_results:
                merge_result(r.get())

        else:
            sp_result = SpeechBinarizer.binarize_file_single_thread(filename, ark_loaders[0], offsets[0], offsets[1], 0,
                                                                    input_format='scp', output_format=output_format,
                                                                    prev_context=prev_context, concat=concat,
                                                                    stride=stride, fp16=fp16)
            merge_result(sp_result)

        final_result['data'] = list()
        final_result['sizes'] = list()

        # put the data into the list according the worker indices
        for idx in range(num_workers):

            for j in range(len(result[idx]['data'])):
                x = result[idx]['data'][j]

                # if we store the numpy array, then convert to torch
                # otherwise, x is the scp path to the matrix
                if isinstance(x, np.ndarray):
                    x = torch.from_numpy(x)
                final_result['data'].append(x)
            final_result['sizes'] += result[idx]['sizes']

        # remember to close the workers when its done
        for i in range(num_workers):
            ark_loaders[i].close()

        return final_result


class Binarizer:

    def __init__(self):
        pass

    @staticmethod
    def find_offsets(filename, num_chunks):
        """
        :param filename: string
        :param num_chunks: int
        :return: a list of offsets (positions to start and stop reading)
        """
        with open(filename, 'r', encoding='utf-8') as f:
            size = os.fstat(f.fileno()).st_size
            chunk_size = size // num_chunks
            offsets = [0 for _ in range(num_chunks + 1)]
            for i in range(1, num_chunks):
                f.seek(chunk_size * i)
                safe_readline(f)
                offsets[i] = f.tell()
            return offsets

    @staticmethod
    def binarize_file_single_thread(filename, tokenizer, vocab, worker_id=0, bos_word=None, eos_word=None,
                                    offset=0, end=-1, data_type='int64', verbose=False,
                                    external_tokenizer=""):
        """
        This function should read in the lines, convert sentences to tensors
        And then finalize into a dataset?
        """

        result = dict()
        unk_word = onmt.constants.UNK_WORD

        data = list()
        sizes = list()

        count = 0

        if "bart" in external_tokenizer.lower():
            if worker_id == 0:
                print("[INFO] Using the external BART tokenizer...")

            from transformers import BartTokenizer
            ext_tokenizer = BartTokenizer.from_pretrained(external_tokenizer)

        elif external_tokenizer is None or len(external_tokenizer) == 0:
            ext_tokenizer = None
        else:
            raise NotImplementedError

        with open(filename, 'r', encoding='utf-8') as f:
            f.seek(offset)

            # next(f) breaks f.tell(), hence readline() must be used
            line = safe_readline(f)

            while line:

                if 0 < end < f.tell():
                    break

                if ext_tokenizer is None:
                    tokenized_sent = tokenizer.tokenize(line)

                    binarized_line = vocab.convertToIdx(tokenized_sent, unk_word,
                                                        bos_word=bos_word, eos_word=eos_word, type=data_type)

                    # move to shared_memory to transfer between threads
                    # conversion to numpy is necessary because torch.Tensor is not serializable by the mprocess
                    data += [binarized_line.numpy()]
                    sizes += [len(tokenized_sent)]
                else:
                    tensor = ext_tokenizer(line)['input_ids']
                    sizes += [len(tensor)]
                    data += [np.asarray(tensor)]

                line = f.readline()

                count += 1
                if count % 100000 == 0:
                    if verbose:
                        print("[INFO] Thread %d processed %d lines." % (worker_id, count))

        if verbose:
            print("[INFO] Thread %d Done." % worker_id)
        result['data'] = data
        result['sizes'] = sizes
        result['id'] = worker_id
        result['total'] = len(sizes)

        return result

    @staticmethod
    def binarize_file(filename, vocab, tokenizer, bos_word=None, eos_word=None,
                      data_type='int64', num_workers=1, verbose=False, external_tokenizer=""):

        result = dict()

        for i in range(num_workers):
            result[i] = dict()

        final_result = dict()

        def merge_result(bin_result):
            result[bin_result['id']]['data'] = bin_result['data']
            result[bin_result['id']]['sizes'] = bin_result['sizes']

        offsets = Binarizer.find_offsets(filename, num_workers)

        if num_workers > 1:

            pool = mp.Pool(processes=num_workers)
            mp_results = []

            for worker_id in range(num_workers):
                mp_results.append(pool.apply_async(
                    Binarizer.binarize_file_single_thread,
                    args=(filename, tokenizer, vocab, worker_id, bos_word, eos_word,
                          offsets[worker_id], offsets[worker_id + 1], data_type, verbose, external_tokenizer),
                ))

            pool.close()
            pool.join()

            for r in mp_results:
                merge_result(r.get())

        else:
            sp_result = Binarizer.binarize_file_single_thread(filename, tokenizer, vocab, 0, bos_word, eos_word,
                                                              offsets[0], offsets[1], data_type,
                                                              external_tokenizer=external_tokenizer)
            merge_result(sp_result)

        final_result['data'] = list()
        final_result['sizes'] = list()

        # put the data into the list according the worker indices
        for idx in range(num_workers):
            final_result['data'] += result[idx]['data']
            final_result['sizes'] += result[idx]['sizes']

        return final_result
