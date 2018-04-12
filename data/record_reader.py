# reader.py
# Data reader
# author: Playinf
# email: playinf@stu.xmu.edu.cn

import six
import math
import tensorflow as tf

from tensorflow.contrib.slim import parallel_reader, tfexample_decoder


def input_pipeline(file_pattern, mode, capacity=64):
    """ Input pipeline, returns a dictionary of tensors from queues. """

    keys_to_features = {
        "inputs": tf.VarLenFeature(tf.int64),
        "preds": tf.VarLenFeature(tf.int64),
        "targets": tf.VarLenFeature(tf.int64)
    }

    items_to_handlers = {
        "inputs": tfexample_decoder.Tensor("inputs"),
        "preds": tfexample_decoder.Tensor("preds"),
        "targets": tfexample_decoder.Tensor("targets")
    }

    # Now the non-trivial case construction.
    with tf.name_scope("examples_queue"):
        training = (mode == tf.contrib.learn.ModeKeys.TRAIN)
        # Read serialized examples using slim parallel_reader.
        num_epochs = None if training else 1
        data_files = parallel_reader.get_data_files(file_pattern)
        num_readers = min(4 if training else 1, len(data_files))
        _, examples = parallel_reader.parallel_read([file_pattern],
                                                    tf.TFRecordReader,
                                                    num_epochs=num_epochs,
                                                    shuffle=training,
                                                    capacity=2 * capacity,
                                                    min_after_dequeue=capacity,
                                                    num_readers=num_readers)

        decoder = tfexample_decoder.TFExampleDecoder(keys_to_features,
                                                     items_to_handlers)

        decoded = decoder.decode(examples, items=list(items_to_handlers))
        examples = {}

        for (field, tensor) in zip(keys_to_features, decoded):
            examples[field] = tensor

        # We do not want int64s as they do are not supported on GPUs.
        return {k: tf.to_int32(v) for (k, v) in six.iteritems(examples)}


def batch_examples(examples, batch_size, max_length, mantissa_bits,
                   shard_multiplier=1, length_multiplier=1, scheme="token",
                   drop_long_sequences=True):
    with tf.name_scope("batch_examples"):
        max_length = max_length or batch_size
        min_length = 8
        mantissa_bits = mantissa_bits

        # compute boundaries
        x = min_length
        boundaries = []

        while x < max_length:
            boundaries.append(x)
            x += 2 ** max(0, int(math.log(x, 2)) - mantissa_bits)

        if scheme is "token":
            batch_sizes = [max(1, batch_size // length)
                           for length in boundaries + [max_length]]
            batch_sizes = [b * shard_multiplier for b in batch_sizes]
            bucket_capacities = [2 * b for b in batch_sizes]
        else:
            batch_sizes = batch_size * shard_multiplier
            bucket_capacities = [2 * n for n in boundaries + [max_length]]

        max_length *= length_multiplier
        boundaries = [boundary * length_multiplier for boundary in boundaries]
        max_length = max_length if drop_long_sequences else 10**9

        # The queue to bucket on will be chosen based on maximum length.
        max_example_length = 0
        for v in examples.values():
            seq_length = tf.shape(v)[0]
            max_example_length = tf.maximum(max_example_length, seq_length)

        (_, outputs) = tf.contrib.training.bucket_by_sequence_length(
            max_example_length,
            examples,
            batch_sizes,
            [b + 1 for b in boundaries],
            capacity=2,  # Number of full batches to store, we don't need many.
            bucket_capacities=bucket_capacities,
            dynamic_pad=True,
            keep_input=(max_example_length <= max_length)
        )

    return outputs


def get_input_fn(file_patterns, mode, params):
    # () -> {features, targets}
    def input_fn():
        with tf.name_scope("input_queues"):
            with tf.device("/cpu:0"):
                if mode != tf.contrib.learn.ModeKeys.TRAIN:
                    num_datashards = 1
                    batch_size = params.eval_batch_size
                else:
                    num_datashards = len(params.device_list)
                    batch_size = params.batch_size

                batch_size_multiplier = 1
                capacity = 64 * num_datashards
                examples = input_pipeline(file_patterns, mode, capacity)
                drop_long_sequences = (mode == tf.contrib.learn.ModeKeys.TRAIN)

                feature_map = batch_examples(
                    examples,
                    batch_size,
                    params.max_length,
                    params.mantissa_bits,
                    num_datashards,
                    batch_size_multiplier,
                    params.batching_scheme,
                    drop_long_sequences
                )

        # Final feature map.
        features = {
            "inputs": feature_map["inputs"],
            "preds": feature_map["preds"]
        }

        targets = feature_map["targets"]

        return features, targets

    return input_fn
