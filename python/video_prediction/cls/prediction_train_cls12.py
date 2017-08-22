import os
import numpy as np
import tensorflow as tf
import sys
import cPickle
import pdb
import imp

from video_prediction.utils_vpred.adapt_params_visualize import adapt_params_visualize
from tensorflow.python.platform import app
from tensorflow.python.platform import flags

from prediction_model_cls12 import Prediction_Model
import makegifs

from datetime import datetime

# How often to record tensorboard summaries.
SUMMARY_INTERVAL = 40

# How often to run a batch through the validation model.
VAL_INTERVAL = 200

# How often to save a model checkpoint
SAVE_INTERVAL = 2000

FLAGS = flags.FLAGS
flags.DEFINE_string('hyper', '', 'hyperparameters configuration file')
flags.DEFINE_string('visualize', '', 'model within hyperparameter folder from which to create gifs')
flags.DEFINE_integer('device', 0 ,'the value for CUDA_VISIBLE_DEVICES variable, -1 uses cpu')
flags.DEFINE_string('pretrained', None, 'path to model file from which to resume training')


def mean_squared_error(true, pred):
    """L2 distance between tensors true and pred.

    Args:
      true: the ground truth image.
      pred: the predicted image.
    Returns:
      mean squared error between ground truth and predicted image.
    """
    return tf.reduce_sum(tf.square(true - pred)) / tf.to_float(tf.size(pred))


class Model(object):
    def __init__(self,
                 conf,
                 images=None,
                 actions=None,
                 states=None,
                 reuse_scope=None,
                 pix_distrib=None):

        self.conf = conf

        if 'use_len' in conf:
            print  'randomly shift videos for data augmentation'
            images, states, actions  = self.random_shift(images, states, actions)

        self.iter_num = tf.placeholder(tf.float32, [])
        summaries = []

        # Split into timesteps.
        if actions != None:
            actions = tf.split(axis=1, num_or_size_splits=actions.get_shape()[1], value=actions)
            actions = [tf.squeeze(act) for act in actions]
        if states != None:
            states = tf.split(axis=1, num_or_size_splits=states.get_shape()[1], value=states)
            states = [tf.squeeze(st) for st in states]
        images = tf.split(axis=1, num_or_size_splits=images.get_shape()[1], value=images)
        images = [tf.squeeze(img) for img in images]
        if pix_distrib != None:
            pix_distrib = tf.split(axis=1, num_or_size_splits=pix_distrib.get_shape()[1], value=pix_distrib)
            pix_distrib = [tf.squeeze(pix) for pix in pix_distrib]

        if reuse_scope is None:
            self.m = Prediction_Model(
                images,
                actions,
                states,
                pix_distrib,
                iter_num=self.iter_num,
                conf=conf)
            self.m.build()
        else:  # If it's a validation or test model.
            with tf.variable_scope(reuse_scope, reuse=True):
                self.m = Prediction_Model(
                    images,
                    actions,
                    states,
                    pix_distrib,
                    iter_num=self.iter_num,
                    conf=conf)
                self.m.build()

        # L2 loss, PSNR for eval.
        true_fft_list, pred_fft_list = [], []
        loss, psnr_all = 0.0, 0.0

        self.fft_weights = tf.placeholder(tf.float32, [64, 64])

        for i, x, gx in zip(
                range(len(self.m.gen_images)), images[conf['context_frames']:],
                self.m.gen_images[conf['context_frames'] - 1:]):
            recon_cost_mse = mean_squared_error(x, gx)
            summaries.append(
                tf.summary.scalar('recon_cost' + str(i), recon_cost_mse))
            recon_cost = recon_cost_mse
            loss += recon_cost

        for i, state, gen_state in zip(
                range(len(self.m.gen_states)), states[conf['context_frames']:],
                self.m.gen_states[conf['context_frames'] - 1:]):
            state_cost = mean_squared_error(state, gen_state) * 1e-4 * conf['use_state']
            summaries.append(
                tf.summary.scalar('state_cost' + str(i), state_cost))
            loss += state_cost

        if 'mask_distinction_loss' in conf:
            dcost = self.distinction_loss(self.om.objectmasks) * conf['mask_distinction_loss']
            summaries.append(
                tf.summary.scalar('mask_distinction_cost', dcost))
            loss += dcost

        self.loss = loss = loss / np.float32(len(images) - conf['context_frames'])

        summaries.append(tf.summary.scalar('_loss', loss))

        self.lr = tf.placeholder_with_default(conf['learning_rate'], ())
        self.train_op = tf.train.AdamOptimizer(self.lr).minimize(loss)
        self.summ_op = tf.summary.merge(summaries)

    def random_shift(self, images, states, actions):
        print 'shifting the video sequence randomly in time'
        tshift = 2
        uselen = self.conf['use_len']
        fulllength = self.conf['sequence_length']
        nshifts = (fulllength - uselen) / 2 + 1
        rand_ind = tf.random_uniform([1], 0, nshifts, dtype=tf.int64)
        self.rand_ind = rand_ind

        start = tf.concat(axis=0, values=[tf.zeros(1, dtype=tf.int64), rand_ind * tshift, tf.zeros(3, dtype=tf.int64)])
        images_sel = tf.slice(images, start, [-1, uselen, -1, -1, -1])
        start = tf.concat(axis=0, values=[tf.zeros(1, dtype=tf.int64), rand_ind * tshift, tf.zeros(1, dtype=tf.int64)])
        actions_sel = tf.slice(actions, start, [-1, uselen, -1])
        start = tf.concat(axis=0, values=[tf.zeros(1, dtype=tf.int64), rand_ind * tshift, tf.zeros(1, dtype=tf.int64)])
        states_sel = tf.slice(states, start, [-1, uselen, -1])

        return images_sel, states_sel, actions_sel

    def distinction_loss(self, masks):
        delta = 0.
        for i in range(self.conf['num_masks']):
            for j in range(self.conf['num_masks']):
                if i == j:
                    continue
                delta -= tf.reduce_sum(tf.abs(masks[i]-masks[j]))
        return delta

def main(unused_argv, conf_script= None):

    if FLAGS.device ==-1:   # using cpu!
        tfconfig = tf.ConfigProto(
            device_count={'GPU': 0}
        )
    else:
        print 'using CUDA_VISIBLE_DEVICES=', FLAGS.device
        os.environ["CUDA_VISIBLE_DEVICES"] = str(FLAGS.device)
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.9)
        tfconfig = tf.ConfigProto(gpu_options=gpu_options)

        from tensorflow.python.client import device_lib
        print device_lib.list_local_devices()

    if conf_script == None: conf_file = FLAGS.hyper
    else: conf_file = conf_script

    if not os.path.exists(FLAGS.hyper):
        sys.exit("Experiment configuration not found")
    hyperparams = imp.load_source('hyperparams', conf_file)

    conf = hyperparams.configuration
    if FLAGS.visualize:
        print 'creating visualizations ...'
        conf = adapt_params_visualize(conf, FLAGS.visualize)
        conf.pop('use_len', None)
        conf['sequence_length'] = 15

    print '-------------------------------------------------------------------'
    print 'verify current settings!! '
    for key in conf.keys():
        print key, ': ', conf[key]
    print '-------------------------------------------------------------------'

    if 'sawyer' in conf:
        from video_prediction.sawyer.read_tf_record_sawyer12 import build_tfrecord_input
    else:
        from video_prediction.read_tf_record import build_tfrecord_input

    print 'Constructing models and inputs.'
    with tf.variable_scope('model', reuse=None) as training_scope:
        images, actions, states = build_tfrecord_input(conf, training=True)
        model = Model(conf, images, actions, states)

    with tf.variable_scope('val_model', reuse=None):
        val_images, val_actions, val_states = build_tfrecord_input(conf, training=False)
        val_model = Model(conf, val_images, val_actions, val_states,
                           training_scope)

    print 'Constructing saver.'
    # Make saver.
    saver = tf.train.Saver(tf.get_collection(tf.GraphKeys.VARIABLES), max_to_keep=0)


    # Make training session.
    sess = tf.InteractiveSession(config= tfconfig)
    summary_writer = tf.summary.FileWriter(
        conf['output_dir'], graph=sess.graph, flush_secs=10)

    tf.train.start_queue_runners(sess)
    sess.run(tf.global_variables_initializer())

    if conf['visualize']:
        saver.restore(sess, conf['visualize'])

        feed_dict = {val_model.lr: 0.0,
                     val_model.iter_num: 0 }
        file_path = conf['output_dir']

        ground_truth, gen_images, gen_masks, gen_pix_distrib, moved_parts, flow_vectors = sess.run([val_images,
                                                        val_model.m.gen_images,
                                                        val_model.m.gen_masks,
                                                        val_model.m.gen_pix_distrib,
                                                        val_model.m.moved_parts,
                                                        val_model.m.flow_vectors
                                                            ],
                                                           feed_dict)

        dict = {}
        dict['gen_images'] = gen_images
        dict['ground_truth'] = ground_truth
        dict['gen_masks'] = gen_masks
        dict['gen_pix_distrib'] = gen_pix_distrib
        dict['moved_parts'] = moved_parts
        dict['flow_vectors'] = flow_vectors

        cPickle.dump(dict, open(file_path + '/pred.pkl','wb'))
        print 'written files to:' + file_path

        makegifs.comp_gif(conf, conf['output_dir'], append_masks=True, show_parts=True)
        return

    itr_0 =0

    if FLAGS.pretrained != None:
        conf['pretrained_model'] = FLAGS.pretrained

        saver.restore(sess, conf['pretrained_model'])
        # resume training at iteration step of the loaded model:
        import re
        itr_0 = re.match('.*?([0-9]+)$', conf['pretrained_model']).group(1)
        itr_0 = int(itr_0)
        print 'resuming training at iteration:  ', itr_0

    tf.logging.info('iteration number, cost')

    starttime = datetime.now()
    t_iter = []
    # Run training.
    fft_weights = calc_fft_weight()

    for itr in range(itr_0, conf['num_iterations'], 1):
        t_startiter = datetime.now()
        # Generate new batch of data_files.
        feed_dict = {
                     model.iter_num: np.float32(itr),
                     model.lr: conf['learning_rate'],
                     model.fft_weights: fft_weights}
        cost, _, summary_str = sess.run([model.loss, model.train_op, model.summ_op],
                                        feed_dict)

        # Print info: iteration #, cost.
        if (itr) % 10 ==0:
            tf.logging.info(str(itr) + ' ' + str(cost))

        if (itr) % VAL_INTERVAL == 2:
            # Run through validation set.
            feed_dict = {val_model.lr: 0.0,
                         val_model.iter_num: np.float32(itr),
                         val_model.fft_weights: fft_weights}
            _, val_summary_str = sess.run([val_model.train_op, val_model.summ_op],
                                          feed_dict)
            summary_writer.add_summary(val_summary_str, itr)


        if (itr) % SAVE_INTERVAL == 2:
            tf.logging.info('Saving model to' + conf['output_dir'])
            saver.save(sess, conf['output_dir'] + '/model' + str(itr))

        t_iter.append((datetime.now() - t_startiter).seconds * 1e6 +  (datetime.now() - t_startiter).microseconds )

        if itr % 100 == 1:
            hours = (datetime.now() -starttime).seconds/3600
            tf.logging.info('running for {0}d, {1}h, {2}min'.format(
                (datetime.now() - starttime).days,
                hours,
                (datetime.now() - starttime).seconds/60 - hours*60))
            avg_t_iter = np.sum(np.asarray(t_iter))/len(t_iter)
            tf.logging.info('time per iteration: {0}'.format(avg_t_iter/1e6))
            tf.logging.info('expected for complete training: {0}h '.format(avg_t_iter /1e6/3600 * conf['num_iterations']))

        if (itr) % SUMMARY_INTERVAL:
            summary_writer.add_summary(summary_str, itr)

    tf.logging.info('Saving model.')
    saver.save(sess, conf['output_dir'] + '/model')
    tf.logging.info('Training complete')
    tf.logging.flush()


def calc_fft_weight():

    weight = np.zeros((64,64))
    for row in range(64):
        for col in range(64):
            p = np.array([row,col])
            c = np.array([31,31])
            weight[row, col] = np.linalg.norm(p -c)**2

    weight /= np.max(weight)
    return weight

if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.INFO)
    app.run()
