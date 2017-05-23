import os
import numpy as np
import tensorflow as tf
import sys
import cPickle
import pdb

from PIL import Image

import imp

from video_prediction.utils_vpred.adapt_params_visualize import adapt_params_visualize
from tensorflow.python.platform import app
from tensorflow.python.platform import flags
from tensorflow.python.platform import gfile
from video_prediction.utils_vpred.create_gif import *
from video_prediction.read_tf_record import add_visuals_to_batch

from video_prediction.read_tf_record import build_tfrecord_input
from video_prediction.utils_vpred.create_gif import assemble_gif

from retina_model import construct_model

import makegifs
from datetime import datetime

# How often to record tensorboard summaries.
SUMMARY_INTERVAL = 40

# How often to run a batch through the validation model.
VAL_INTERVAL = 200

# How often to save a model checkpoint
SAVE_INTERVAL = 2000

if __name__ == "__main__":
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
                 highres_images = None,
                 actions=None,
                 states=None,
                 init_retpos=None,
                 reuse_scope=None,
                 ):

        self.conf = conf

        self.prefix = prefix = tf.placeholder(tf.string, [])
        self.iter_num = tf.placeholder(tf.float32, [])
        summaries = []

        # Split into timesteps.
        if actions != None:
            actions = tf.split(1, actions.get_shape()[1], actions)
            actions = [tf.squeeze(act) for act in actions]
        if states != None:
            states = tf.split(1, states.get_shape()[1], states)
            states = [tf.squeeze(st) for st in states]
        images = tf.split(1, images.get_shape()[1], images)
        images = [tf.squeeze(img) for img in images]
        highres_images = tf.split(1, highres_images.get_shape()[1], highres_images)
        highres_images = [tf.squeeze(img) for img in highres_images]

        self.init_pixdistrib = self.make_initial_pixdistrib()

        if reuse_scope is None:
            gen_retina, gen_states, gen_pix_distrib, true_retina, retina_pos, maxcoord = construct_model(
                images,
                highres_images,
                actions,
                states,
                init_retina_pos = init_retpos,
                pix_distributions= self.init_pixdistrib,
                iter_num=self.iter_num,
                k=conf['schedsamp_k'],
                use_state=conf['use_state'],
                num_masks=conf['num_masks'],
                cdna=conf['model'] == 'CDNA',
                dna=conf['model'] == 'DNA',
                stp=conf['model'] == 'STP',
                context_frames=conf['context_frames'],
                conf=conf)
        else:  # If it's a validation or test model.
            with tf.variable_scope(reuse_scope, reuse=True):
                gen_retina, gen_states, gen_pix_distrib, true_retina, retina_pos, maxcoord = construct_model(
                    images,
                    highres_images,
                    actions,
                    states,
                    init_retina_pos=init_retpos,
                    pix_distributions=self.init_pixdistrib,
                    iter_num=self.iter_num,
                    k=conf['schedsamp_k'],
                    use_state=conf['use_state'],
                    num_masks=conf['num_masks'],
                    cdna=conf['model'] == 'CDNA',
                    dna=conf['model'] == 'DNA',
                    stp=conf['model'] == 'STP',
                    context_frames=conf['context_frames'],
                    conf= conf)

        loss, psnr_all = 0.0, 0.0

        for i, x, gx in zip(
                range(len(gen_retina)), true_retina[conf['context_frames']:],
                gen_retina[conf['context_frames'] - 1:]):
            recon_cost_mse = mean_squared_error(x, gx)
            summaries.append(
                tf.scalar_summary(prefix + '_recon_cost' + str(i), recon_cost_mse))
            loss += recon_cost_mse

        for i, state, gen_state in zip(
                range(len(gen_states)), states[conf['context_frames']:],
                gen_states[conf['context_frames'] - 1:]):
            state_cost = mean_squared_error(state, gen_state) * conf['state_cost_factor']
            summaries.append(
                tf.scalar_summary(prefix + '_state_cost' + str(i), state_cost))
            loss += state_cost

        self.loss = loss = loss / np.float32(len(images) - conf['context_frames'])
        summaries.append(tf.scalar_summary(prefix + '_loss', loss))

        self.lr = tf.placeholder_with_default(conf['learning_rate'], ())

        self.true_retina = true_retina
        self.retina_pos = retina_pos
        self.maxcoord = maxcoord
        self.gen_retina = gen_retina

        self.train_op = tf.train.AdamOptimizer(self.lr).minimize(loss)
        self.summ_op = tf.merge_summary(summaries)

        self.gen_states = gen_states
        self.gen_pix_distrib = gen_pix_distrib


    def make_initial_pixdistrib(self):
        r = 16
        c = 16

        flat_ind = tf.constant([r*self.conf['retina_size'] + c], dtype= tf.int32)
        flat_ind = tf.tile(flat_ind, [self.conf['batch_size']])
        one_hot = tf.one_hot(flat_ind, depth=self.conf['retina_size']**2, axis = -1)
        one_hot = tf.reshape(one_hot, [self.conf['batch_size'], self.conf['retina_size'], self.conf['retina_size']])

        return [one_hot, one_hot]


def main(conf):

    if FLAGS.device ==-1:   # using cpu!
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        tfconfig = None
    else:
        print 'using CUDA_VISIBLE_DEVICES=', FLAGS.device
        os.environ["CUDA_VISIBLE_DEVICES"] = str(FLAGS.device)
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.9)
        tfconfig = tf.ConfigProto(gpu_options=gpu_options)

        from tensorflow.python.client import device_lib
        print device_lib.list_local_devices()

    conf_file = FLAGS.hyper

    if not os.path.exists(FLAGS.hyper):
        pdb.set_trace()
        sys.exit("Experiment configuration not found")
    hyperparams = imp.load_source('hyperparams', conf_file)

    conf = hyperparams.configuration
    if FLAGS.visualize:
        print 'creating visualizations ...'
        conf['schedsamp_k'] = -1  # don't feed ground truth
        conf['data_dir'] = '/'.join(str.split(conf['data_dir'], '/')[:-1] + ['test'])
        conf['visualize'] = conf['output_dir'] + '/' + FLAGS.visualize
        conf['event_log_dir'] = '/tmp'
        conf['visual_file'] = conf['data_dir'] + '/traj_256_to_511.tfrecords'

    print '-------------------------------------------------------------------'
    print 'verify current settings!! '
    for key in conf.keys():
        print key, ': ', conf[key]
    print '-------------------------------------------------------------------'

    print 'Constructing models and inputs.'
    with tf.variable_scope('model', reuse=None) as training_scope:
        images, highres_images, ret_pos, actions, states, poses = build_tfrecord_input(conf, training=True, shuffle_vis=True)
        model = Model(conf, images,highres_images, actions, states, ret_pos)

    with tf.variable_scope('val_model', reuse=None):
        val_images, val_highres_images, val_ret_pos, val_actions, val_states, val_poses = build_tfrecord_input(conf, training=False, shuffle_vis=True)
        val_model = Model(conf, val_images,val_highres_images, val_actions, val_states, val_ret_pos, training_scope)

    print 'Constructing saver.'
    # Make saver.
    saver = tf.train.Saver(tf.get_collection(tf.GraphKeys.VARIABLES), max_to_keep=0)

    # Make training session.
    sess = tf.InteractiveSession(config= tfconfig)
    summary_writer = tf.train.SummaryWriter(
        conf['output_dir'], graph=sess.graph, flush_secs=10)

    tf.train.start_queue_runners(sess)
    sess.run(tf.initialize_all_variables())

    if conf['visualize']:
        saver.restore(sess, conf['visualize'])

        feed_dict = {val_model.lr: 0.0,
                     val_model.prefix: 'vis',
                     val_model.iter_num: 0 }
        file_path = conf['output_dir']

        val_highres_images_data, gen_retinas, gtruth_retinas, gen_pix_distrib, retina_pos, maxcoord, gtruth_image = sess.run([
                                                                val_highres_images,
                                                                val_model.gen_retina,
                                                                val_model.true_retina,
                                                                val_model.gen_pix_distrib,
                                                                val_model.retina_pos,
                                                                val_model.maxcoord,
                                                                val_images,
                                                                        ],
                                                                       feed_dict)
        dict_ = {}
        dict_['gen_retinas'] = gen_retinas
        dict_['gtruth_retinas'] = gtruth_retinas
        dict_['val_highres_images'] = val_highres_images_data
        dict_['gen_pix_distrib'] = gen_pix_distrib
        dict_['maxcoord'] = maxcoord
        dict_['retina_pos'] = retina_pos

        cPickle.dump(dict_, open(file_path + '/dict_.pkl', 'wb'))
        print 'written files to:' + file_path

        makegifs.comp_pix_distrib(conf['output_dir'], examples=16)
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

    ####### debugging
    # itr = 0
    # feed_dict = {model.prefix: 'train',
    #              model.iter_num: np.float32(itr),
    #              model.lr: conf['learning_rate'],
    #              }
    # init_pix, true_retina, ret_pos_data = sess.run([model.init_pixdistrib, model.true_retina, ret_pos],
    #                                 feed_dict)
    #
    # Image.fromarray((true_retina[0][0] * 255).astype(np.uint8)).show()
    # Image.fromarray((true_retina[4][0] * 255).astype(np.uint8)).show()
    #
    # Image.fromarray((init_pix[0][0] * 255).astype(np.uint8)).show()
    # print 'retina pos:'
    # for i in range(3):
    #      print ret_pos_data[i][0]
    #
    # pdb.set_trace()
    ####### end debugging

    # Run training.

    for itr in range(itr_0, conf['num_iterations'], 1):
        t_startiter = datetime.now()
        # Generate new batch of data_files.
        feed_dict = {model.prefix: 'train',
                     model.iter_num: np.float32(itr),
                     model.lr: conf['learning_rate'],
                     }
        cost, _, summary_str = sess.run([model.loss, model.train_op, model.summ_op],
                                        feed_dict)

        # Print info: iteration #, cost.
        if (itr) % 10 ==0:
            tf.logging.info(str(itr) + ' ' + str(cost))

        if (itr) % VAL_INTERVAL == 2:
            # Run through validation set.
            feed_dict = {val_model.lr: 0.0,
                         val_model.prefix: 'val',
                         val_model.iter_num: np.float32(itr),
                         }
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

if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.INFO)
    app.run()


