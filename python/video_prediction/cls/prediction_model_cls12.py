# Copyright 2016 The TensorFlow Authors All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Model architecture for predictive model, including CDNA, DNA, and STP."""

import numpy as np
import tensorflow as tf

import tensorflow.contrib.slim as slim
from tensorflow.contrib.layers.python import layers as tf_layers
from video_prediction.lstm_ops12 import basic_conv_lstm_cell

import pdb

# Amount to use when lower bounding tensors
RELU_SHIFT = 1e-12

class Prediction_Model(object):
    def __init__(self,
                images,
                actions=None,
                states=None,
                pix_distibution=None,
                iter_num=-1.0,
                conf = None):

        self.pix_distribution = pix_distibution
        self.actions = actions
        self.iter_num = iter_num
        self.conf = conf
        self.images = images

        self.cdna, self.stp, self.dna = False, False, False
        if self.conf['model'] == 'CDNA':
            self.cdna = True
        elif self.conf['model'] == 'DNA':
            self.dna = True
        elif self.conf['model'] == 'STP':
            self.stp = True
        if self.stp + self.cdna + self.dna != 1:
            raise ValueError("More than one option selected!")

        self.k = conf['schedsamp_k']
        self.use_state = conf['use_state']
        self.num_masks = conf['num_masks']
        self.context_frames = conf['context_frames']

        print 'constructing cls network...'

        self.batch_size, self.img_height, self.img_width, self.color_channels = [int(i) for i in images[0].get_shape()[0:4]]
        self.lstm_func = basic_conv_lstm_cell

        # Generated robot states and images.
        self.gen_states = []
        self.gen_images = []
        self.gen_masks = []

        self.moved_parts = []
        self.assembly_masks_list = []
        self.list_of_trafos = []
        self.list_of_comp_factors = []
        self.states = states
        self.gen_pix_distrib = []

        self.flow_vectors = []

    def build(self):

        if 'dna_size' in self.conf.keys():
            DNA_KERN_SIZE = self.conf['dna_size']
        else:
            DNA_KERN_SIZE = 5

        print 'constructing video prediction network ...'


        batch_size, img_height, img_width, color_channels = self.images[0].get_shape()[0:4]
        lstm_func = basic_conv_lstm_cell

        # Generated robot states and images.
        # gen_states, gen_images, gen_masks = [], [], []
        current_state = self.states[0]

        summaries = []

        if self.k == -1:
            feedself = True
        else:
            # Scheduled sampling:
            # Calculate number of ground-truth frames to pass in.
            num_ground_truth = tf.to_int32(
                tf.round(tf.to_float(batch_size) * (self.k / (self.k + tf.exp(self.iter_num / self.k)))))
            feedself = False

        # LSTM state sizes and states.

        if 'lstm_size' in self.conf:
            lstm_size = self.conf['lstm_size']
        else:
            lstm_size = np.int32(np.array([16, 16, 32, 32, 64, 32, 16]))

        lstm_state1, lstm_state2, lstm_state3, lstm_state4 = None, None, None, None
        lstm_state5, lstm_state6, lstm_state7 = None, None, None

        t = -1
        for image, action in zip(self.images[:-1], self.actions[:-1]):
            t +=1
            # Reuse variables after the first timestep.
            reuse = bool(self.gen_images)
            print 't',t

            done_warm_start = len(self.gen_images) > self.context_frames - 1
            with slim.arg_scope(
                    [lstm_func, slim.layers.conv2d, slim.layers.fully_connected,
                     tf_layers.layer_norm, slim.layers.conv2d_transpose],
                    reuse=reuse):

                if feedself and done_warm_start:
                    # Feed in generated image.
                    prev_image = self.gen_images[-1]
                    if self.pix_distribution != None:
                        prev_pix_distrib = self.gen_pix_distrib[-1]
                elif done_warm_start:
                    # Scheduled sampling
                    prev_image = scheduled_sample(image, self.gen_images[-1], batch_size,
                                                  num_ground_truth)
                else:
                    # Always feed in ground_truth
                    prev_image = image
                    if self.pix_distribution != None:
                        prev_pix_distrib = self.pix_distribution[t]
                        prev_pix_distrib = tf.expand_dims(prev_pix_distrib, -1)

                if 'transform_from_firstimage' in self.conf:
                    assert self.stp
                    if t > 1:
                        prev_image = self.images[1]
                        print 'using image 1'

                # Predicted state is always fed back in
                state_action = tf.concat(axis=1, values=[action, current_state])

                # for experiment where the model is actually fed the complete sequence
                if 'provide_gtruth' in self.conf:
                    print 'always feeding in the next ground truth image!!!'
                    conv_input = tf.concat(values=[prev_image, self.images[t + 1]], axis=3)

                enc0 = slim.layers.conv2d(    #32x32x32
                    conv_input,
                    32, [5, 5],
                    stride=2,
                    scope='scale1_conv1',
                    normalizer_fn=tf_layers.layer_norm,
                    normalizer_params={'scope': 'layer_norm1'})

                hidden1, lstm_state1 = lstm_func(       # 32x32x16
                    enc0, lstm_state1, lstm_size[0], scope='state1')
                hidden1 = tf_layers.layer_norm(hidden1, scope='layer_norm2')
                # hidden2, lstm_state2 = lstm_func(
                #     hidden1, lstm_state2, lstm_size[1], scope='state2')
                # hidden2 = tf_layers.layer_norm(hidden2, scope='layer_norm3')
                enc1 = slim.layers.conv2d(     # 16x16x16
                    hidden1, hidden1.get_shape()[3], [3, 3], stride=2, scope='conv2')

                hidden3, lstm_state3 = lstm_func(   #16x16x32
                    enc1, lstm_state3, lstm_size[2], scope='state3')
                hidden3 = tf_layers.layer_norm(hidden3, scope='layer_norm4')
                # hidden4, lstm_state4 = lstm_func(
                #     hidden3, lstm_state4, lstm_size[3], scope='state4')
                # hidden4 = tf_layers.layer_norm(hidden4, scope='layer_norm5')
                enc2 = slim.layers.conv2d(    #8x8x32
                    hidden3, hidden3.get_shape()[3], [3, 3], stride=2, scope='conv3')

                # Pass in state and action.
                smear = tf.reshape(
                    state_action,
                    [int(batch_size), 1, 1, int(state_action.get_shape()[1])])
                smear = tf.tile(
                    smear, [1, int(enc2.get_shape()[1]), int(enc2.get_shape()[2]), 1])
                if self.use_state:
                    enc2 = tf.concat(axis=3, values=[enc2, smear])
                enc3 = slim.layers.conv2d(   #8x8x32
                    enc2, hidden3.get_shape()[3], [1, 1], stride=1, scope='conv4')

                hidden5, lstm_state5 = lstm_func(  #8x8x64
                    enc3, lstm_state5, lstm_size[4], scope='state5')
                hidden5 = tf_layers.layer_norm(hidden5, scope='layer_norm6')
                enc4 = slim.layers.conv2d_transpose(  #16x16x64
                    hidden5, hidden5.get_shape()[3], 3, stride=2, scope='convt1')

                hidden6, lstm_state6 = lstm_func(  #16x16x32
                    enc4, lstm_state6, lstm_size[5], scope='state6')
                hidden6 = tf_layers.layer_norm(hidden6, scope='layer_norm7')

                if not 'noskip' in self.conf:
                    # Skip connection.
                    hidden6 = tf.concat(axis=3, values=[hidden6, enc1])  # both 16x16

                enc5 = slim.layers.conv2d_transpose(  #32x32x32
                    hidden6, hidden6.get_shape()[3], 3, stride=2, scope='convt2')
                hidden7, lstm_state7 = lstm_func( # 32x32x16
                    enc5, lstm_state7, lstm_size[6], scope='state7')
                hidden7 = tf_layers.layer_norm(hidden7, scope='layer_norm8')

                if not 'noskip' in self.conf:
                    # Skip connection.
                    hidden7 = tf.concat(axis=3, values=[hidden7, enc0])  # both 32x32

                enc6 = slim.layers.conv2d_transpose(   # 64x64x16
                    hidden7,
                    hidden7.get_shape()[3], 3, stride=2, scope='convt3',
                    normalizer_fn=tf_layers.layer_norm,
                    normalizer_params={'scope': 'layer_norm9'})

                if self.dna:
                    # Using largest hidden state for predicting untied conv kernels.
                    enc7 = slim.layers.conv2d_transpose(
                        enc6, DNA_KERN_SIZE ** 2, 1, stride=1, scope='convt4')
                else:
                    # Using largest hidden state for predicting a new image layer.
                    enc7 = slim.layers.conv2d_transpose(
                        enc6, color_channels, 1, stride=1, scope='convt4')
                    # This allows the network to also generate one image from scratch,
                    # which is useful when regions of the image become unoccluded.

                    if 'no_gen_pix' not in self.conf:
                        transformed = [tf.nn.sigmoid(enc7)]
                    else:
                        print 'pixel generation disabled!'
                        transformed = []

                if self.stp:
                    stp_input0 = tf.reshape(hidden5, [int(batch_size), -1])
                    stp_input1 = slim.layers.fully_connected(
                        stp_input0, 100, scope='fc_stp')

                    # disabling capability to generete pixels
                    reuse_stp = None
                    if reuse:
                        reuse_stp = reuse
                    transformed = self.stp_transformation(prev_image, stp_input1, self.num_masks, reuse_stp)
                    self.moved_parts.append(transformed)
                    # transformed += stp_transformation(prev_image, stp_input1, num_masks)

                    if self.pix_distribution != None:
                        transf_distrib = self.stp_transformation(prev_pix_distrib, stp_input1, self.num_masks, reuse=True)

                elif self.cdna:
                    cdna_input = tf.reshape(hidden5, [int(batch_size), -1])

                    new_transformed, cdna_kerns = self.cdna_transformation(prev_image,
                                                                    cdna_input,
                                                                    reuse_sc= reuse)

                    transformed += new_transformed
                    self.moved_parts.append(transformed)
                    if self.pix_distribution != None:
                        transf_distrib, _ = self.cdna_transformation(prev_pix_distrib,
                                                                    cdna_input,
                                                                   reuse_sc= True)

                elif self.dna:
                    # Only one mask is supported (more should be unnecessary).
                    if self.num_masks != 1:
                        raise ValueError('Only one mask is supported for DNA model.')
                    transformed = [self.dna_transformation(prev_image, enc7, DNA_KERN_SIZE)]

                masks = slim.layers.conv2d_transpose(
                    enc6, self.num_masks + 1, 1, stride=1, scope='convt7')
                masks = tf.reshape(
                    tf.nn.softmax(tf.reshape(masks, [-1, self.num_masks + 1])),
                    [int(batch_size), int(img_height), int(img_width), self.num_masks + 1])
                mask_list = tf.split(axis=3, num_or_size_splits=self.num_masks + 1, value=masks)
                output = mask_list[0] * prev_image
                for layer, mask in zip(transformed, mask_list[1:]):
                    output += layer * mask
                self.gen_images.append(output)
                self.gen_masks.append(mask_list)

                if self.dna and self.pix_distribution != None:
                    transf_distrib = [self.dna_transformation(prev_pix_distrib, enc7, DNA_KERN_SIZE)]

                if self.pix_distribution !=None:
                    pix_distrib_output = mask_list[0] * prev_pix_distrib
                    mult_list = []
                    for i in range(self.num_masks):
                        mult_list.append(transf_distrib[i] * mask_list[i+1])
                        pix_distrib_output += mult_list[i]

                    self.gen_pix_distrib.append(pix_distrib_output)

                if 'visual_flowvec' in self.conf:
                    motion_vecs = self.compute_motion_vector(cdna_kerns)
                    output = tf.zeros([self.conf['batch_size'],64,64,2])
                    for vec, mask in zip(motion_vecs, mask_list[1:]):
                        vec = tf.reshape(vec, [32, 1, 1, 2])
                        vec = tf.tile(vec, [1, 64,64, 1])
                        output += vec * mask

                    self.flow_vectors.append(output)

                current_state = slim.layers.fully_connected(
                    state_action,
                    int(current_state.get_shape()[1]),
                    scope='state_pred',
                    activation_fn=None)
                self.gen_states.append(current_state)

    def make_cdna_kerns_summary(self,cdna_kerns, t, suffix):

        sum = []
        cdna_kerns = tf.split(axis=4, num_or_size_splits=self.num_masks, value=cdna_kerns)
        for i, kern in enumerate(cdna_kerns):
            kern = tf.squeeze(kern)
            kern = tf.expand_dims(kern, -1)
            sum.append(
                tf.summary.image('step' + str(t) + '_filter' + str(i) + suffix, kern)
            )

        return sum

    def compute_motion_vector(self, cdna_kerns):

        range = self.conf['kern_size'] / 2
        dc = np.linspace(-range, range, num= self.conf['kern_size'])
        dc = np.expand_dims(dc, axis=0)
        dc = np.repeat(dc, self.conf['kern_size'], axis=0)
        dr = np.transpose(dc)
        dr = tf.constant(dr, dtype=tf.float32)
        dc = tf.constant(dc, dtype=tf.float32)

        cdna_kerns = tf.transpose(cdna_kerns, [2, 3, 0, 1])
        cdna_kerns = tf.split(cdna_kerns, self.conf['num_masks'], axis=1)
        cdna_kerns = [tf.squeeze(k) for k in cdna_kerns]

        vecs = []
        for kern in cdna_kerns:
            vec_r = tf.multiply(dr, kern)
            vec_r = tf.reduce_sum(vec_r, axis=[1,2])
            vec_c = tf.multiply(dc, kern)
            vec_c = tf.reduce_sum(vec_c, axis=[1, 2])

            vecs.append(tf.stack([vec_r,vec_c], axis=1))
        return vecs


    ## Utility functions
    def stp_transformation(self,prev_image, stp_input, num_masks, reuse= None):
        """Apply spatial transformer predictor (STP) to previous image.

        Args:
          prev_image: previous image to be transformed.
          stp_input: hidden layer to be used for computing STN parameters.
          num_masks: number of masks and hence the number of STP transformations.
        Returns:
          List of images transformed by the predicted STP parameters.
        """
        # Only import spatial transformer if needed.
        from video_prediction.transformer.spatial_transformer import transformer

        identity_params = tf.convert_to_tensor(
            np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0], np.float32))
        transformed = []
        for i in range(num_masks):
            params = slim.layers.fully_connected(
                stp_input, 6, scope='stp_params' + str(i),
                activation_fn=None,
                reuse= reuse) + identity_params
            outsize = (prev_image.get_shape()[1], prev_image.get_shape()[2])
            transformed.append(transformer(prev_image, params, outsize))

        return transformed


    def cdna_transformation(self, prev_image, cdna_input, reuse_sc=None):
        """Apply convolutional dynamic neural advection to previous image.

        Args:
          prev_image: previous image to be transformed.
          cdna_input: hidden lyaer to be used for computing CDNA kernels.
          num_masks: the number of masks and hence the number of CDNA transformations.
          color_channels: the number of color channels in the images.
        Returns:
          List of images transformed by the predicted CDNA kernels.
        """
        DNA_KERN_SIZE = self.conf['kern_size']
        num_masks = self.conf['num_masks']
        color_channels = int(prev_image.get_shape()[3])

        batch_size = int(cdna_input.get_shape()[0])
        height = int(prev_image.get_shape()[1])
        width = int(prev_image.get_shape()[2])

        # Predict kernels using linear function of last hidden layer.
        cdna_kerns = slim.layers.fully_connected(
            cdna_input,
            DNA_KERN_SIZE * DNA_KERN_SIZE * num_masks,
            scope='cdna_params',
            activation_fn=None,
            reuse=reuse_sc)

        # Reshape and normalize.
        cdna_kerns = tf.reshape(
            cdna_kerns, [batch_size, DNA_KERN_SIZE, DNA_KERN_SIZE, 1, num_masks])
        cdna_kerns = tf.nn.relu(cdna_kerns - RELU_SHIFT) + RELU_SHIFT
        norm_factor = tf.reduce_sum(cdna_kerns, [1, 2, 3], keep_dims=True)
        cdna_kerns /= norm_factor
        cdna_kerns_summary = cdna_kerns

        # Transpose and reshape.
        cdna_kerns = tf.transpose(cdna_kerns, [1, 2, 0, 4, 3])
        cdna_kerns = tf.reshape(cdna_kerns, [DNA_KERN_SIZE, DNA_KERN_SIZE, batch_size, num_masks])
        prev_image = tf.transpose(prev_image, [3, 1, 2, 0])

        transformed = tf.nn.depthwise_conv2d(prev_image, cdna_kerns, [1, 1, 1, 1], 'SAME')

        # Transpose and reshape.
        transformed = tf.reshape(transformed, [color_channels, height, width, batch_size, num_masks])
        transformed = tf.transpose(transformed, [3, 1, 2, 0, 4])
        transformed = tf.unstack(value=transformed, axis=-1)

        return transformed, cdna_kerns

    def dna_transformation(self, prev_image, dna_input, DNA_KERN_SIZE):
        """Apply dynamic neural advection to previous image.

        Args:
          prev_image: previous image to be transformed.
          dna_input: hidden lyaer to be used for computing DNA transformation.
        Returns:
          List of images transformed by the predicted CDNA kernels.
        """
        # Construct translated images.
        pad_len = int(np.floor(DNA_KERN_SIZE / 2))
        prev_image_pad = tf.pad(prev_image, [[0, 0], [pad_len, pad_len], [pad_len, pad_len], [0, 0]])
        image_height = int(prev_image.get_shape()[1])
        image_width = int(prev_image.get_shape()[2])

        inputs = []
        for xkern in range(DNA_KERN_SIZE):
            for ykern in range(DNA_KERN_SIZE):
                inputs.append(
                    tf.expand_dims(
                        tf.slice(prev_image_pad, [0, xkern, ykern, 0],
                                 [-1, image_height, image_width, -1]), [3]))
        inputs = tf.concat(axis=3, values=inputs)

        # Normalize channels to 1.
        kernel = tf.nn.relu(dna_input - RELU_SHIFT) + RELU_SHIFT
        kernel = tf.expand_dims(
            kernel / tf.reduce_sum(
                kernel, [3], keep_dims=True), [4])

        return tf.reduce_sum(kernel * inputs, [3], keep_dims=False)


def scheduled_sample(ground_truth_x, generated_x, batch_size, num_ground_truth):
    """Sample batch with specified mix of ground truth and generated data_files points.

    Args:
      ground_truth_x: tensor of ground-truth data_files points.
      generated_x: tensor of generated data_files points.
      batch_size: batch size
      num_ground_truth: number of ground-truth examples to include in batch.
    Returns:
      New batch with num_ground_truth sampled from ground_truth_x and the rest
      from generated_x.
    """
    idx = tf.random_shuffle(tf.range(int(batch_size)))
    ground_truth_idx = tf.gather(idx, tf.range(num_ground_truth))
    generated_idx = tf.gather(idx, tf.range(num_ground_truth, int(batch_size)))

    ground_truth_examps = tf.gather(ground_truth_x, ground_truth_idx)
    generated_examps = tf.gather(generated_x, generated_idx)
    return tf.dynamic_stitch([ground_truth_idx, generated_idx],
                             [ground_truth_examps, generated_examps])



