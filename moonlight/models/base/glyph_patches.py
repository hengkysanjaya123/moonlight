# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Base patch-based glyph model.

For example, this accepts the staff patch k-means centroids emitted by
staffline_patches_kmeans_pipeline and labeled by kmeans_labeler.

This defines the input and signature of the model, and allows any type of
multi-class classifier using the normalized patches as input.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math

from absl import flags
from moonlight.models.base import batches
from moonlight.models.base import label_weights
from moonlight.util import memoize
import tensorflow as tf
from tensorflow.python.lib.io import file_io
from tensorflow.python.lib.io import tf_record

WEIGHT_COLUMN_NAME = 'weight'

FLAGS = flags.FLAGS

flags.DEFINE_string(
    'train_input_patches', None, 'Glob of labeled patch TFRecords for training')
flags.DEFINE_string(
    'eval_input_patches', None, 'Glob of labeled patch TFRecords for eval')
flags.DEFINE_string('model_dir', None, 'Output trained model directory')
flags.DEFINE_boolean(
    'use_included_label_weight', False,
    'Whether to multiply a "label_weight" feature included in the example by'
    ' the weight determined by the "label" value.')
flags.DEFINE_float(
    'augmentation_x_shift_probability', 0.5,
    'Probability of shifting the patch left or right by one pixel. The edge is'
    ' filled using the adjacent column. It is equally likely that the patch is'
    ' shifted left or right.')
flags.DEFINE_float(
    'augmentation_max_rotation_degrees', 2.,
    'Max rotation of the patch, in degrees. The rotation is selected uniformly'
    ' randomly from the range +- this value. A value of 0 implies no rotation.')
flags.DEFINE_integer(
    'eval_throttle_secs', 60, 'Evaluate at at most this interval, in seconds.')
flags.DEFINE_integer(
    'train_max_steps', 100000,
    'Max steps for training. If 0, will train until the process is'
    ' interrupted.')


@memoize.MemoizedFunction
def read_patch_dimensions():
  """Reads the dimensions of the input patches from disk.

  Parses the first example in the training set, which must have "height" and
  "width" features.

  Returns:
    Tuple of (height, width) read from disk, using the glob passed to
    --train_input_patches.
  """
  for filename in file_io.get_matching_files(FLAGS.train_input_patches):
    # If one matching file is empty, go on to the next file.
    for record in tf_record.tf_record_iterator(filename):
      example = tf.train.Example.FromString(record)
      # Convert long (int64) to int, necessary for use in feature columns in
      # Python 2.
      patch_height = int(example.features.feature['height'].int64_list.value[0])
      patch_width = int(example.features.feature['width'].int64_list.value[0])
      return patch_height, patch_width


def input_fn(input_patches):
  """Defines the estimator input function.

  Args:
    input_patches: The input patches TFRecords pattern.

  Returns:
    A callable. Each invocation returns a tuple containing:
    * A dict with a single key 'patch', and the patch tensor as a value.
    * A scalar tensor with the patch label, as an integer.
  """
  patch_height, patch_width = read_patch_dimensions()
  dataset = tf.data.TFRecordDataset(file_io.get_matching_files(input_patches))

  def parser(record):
    """Dataset parser function.

    Args:
      record: A single serialized Example proto tensor.

    Returns:
      A tuple of:
      * A dict of features ('patch' and 'weight')
      * A label tensor (int64 scalar).
    """
    feature_types = {
        'patch':
            tf.FixedLenFeature((patch_height, patch_width), tf.float32),
        'label':
            tf.FixedLenFeature((), tf.int64),
    }
    if FLAGS.use_included_label_weight:
      feature_types['label_weight'] = tf.FixedLenFeature((), tf.float32)
    features = tf.parse_single_example(record, feature_types)

    label = features['label']
    weight = label_weights.weights_from_labels(label)
    if FLAGS.use_included_label_weight:
      weight *= features['label_weight']
    patch = _augment(features['patch'])
    return {'patch': patch, WEIGHT_COLUMN_NAME: weight}, label

  return batches.get_batched_tensor(dataset.map(parser))


def _augment(patch):
  """Performs multiple augmentations on the patch, helping to generalize."""
  return _augment_rotation(_augment_shift(patch))


def _augment_shift(patch):
  """Augments the patch by possibly shifting it 1 pixel horizontally."""
  with tf.name_scope('augment_shift'):
    rand = tf.random_uniform(())
    def shift_left():
      return _shift_left(patch)
    def shift_right():
      return _shift_right(patch)
    def identity():
      return patch
    shift_prob = min(1., FLAGS.augmentation_x_shift_probability)
    return tf.cond(rand < shift_prob / 2,
                   shift_left,
                   lambda: tf.cond(rand < shift_prob, shift_right, identity))


def _shift_left(patch):
  patch = tf.convert_to_tensor(patch)
  return tf.concat([patch[:, 1:], patch[:, -1:]], axis=1)


def _shift_right(patch):
  patch = tf.convert_to_tensor(patch)
  return tf.concat([patch[:, :1], patch[:, :-1]], axis=1)


def _augment_rotation(patch):
  """Augments the patch by rotating it by a small amount."""
  max_rotation_radians = math.radians(FLAGS.augmentation_max_rotation_degrees)
  rotation = tf.random_uniform(
      (), minval=-max_rotation_radians, maxval=max_rotation_radians)
  # Background is white (1.0) but tf.contrib.image.rotate currently always fills
  # the edges with black (0). Invert the patch before rotating.
  return 1. - tf.contrib.image.rotate(
      1. - patch, rotation, interpolation='BILINEAR')


def serving_fn():
  """Returns the ServingInputReceiver for the exported model.

  Returns:
    A ServingInputReceiver object which may be passed to
    `Estimator.export_savedmodel`. A model saved using this receiver may be used
    for running OMR.
  """
  examples = tf.placeholder(tf.string, shape=[None])
  patch_height, patch_width = read_patch_dimensions()
  parsed = tf.parse_example(examples, {
      'patch': tf.FixedLenFeature((patch_height, patch_width), tf.float32),
  })
  return tf.estimator.export.ServingInputReceiver(
      features={'patch': parsed['patch']},
      receiver_tensors=parsed['patch'],
      receiver_tensors_alternatives={
          'example': examples,
          'patch': parsed['patch']
      })


def create_patch_feature_column():
  return tf.feature_column.numeric_column(
      'patch', shape=read_patch_dimensions())


def train_and_evaluate(estimator):
  tf.estimator.train_and_evaluate(
      estimator,
      tf.estimator.TrainSpec(
          input_fn=lambda: input_fn(FLAGS.train_input_patches),
          max_steps=FLAGS.train_max_steps),
      tf.estimator.EvalSpec(
          input_fn=lambda: input_fn(FLAGS.eval_input_patches),
          start_delay_secs=0, throttle_secs=FLAGS.eval_throttle_secs,
          exporters=[
              tf.estimator.LatestExporter('exporter', serving_fn),
          ]))
