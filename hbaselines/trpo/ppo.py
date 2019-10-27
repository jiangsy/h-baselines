import gym
import tensorflow as tf
import numpy as np

from stable_baselines.common import Dataset, fmt_row, zipsame
import stable_baselines.common.tf_util as tf_util
from stable_baselines.common.mpi_moments import mpi_moments
from hbaselines.trpo.algorithm import RLAlgorithm


class PPO(RLAlgorithm):
    """Proximal Policy Optimization.

    See: https://arxiv.org/abs/1707.06347

    Attributes
    ----------
    env : gym.Env or str
        The environment to learn from (if registered in Gym, can be str)
    policy : TODO
        The policy model to use
    timesteps_per_batch : int
        timesteps per actor per update
    clip_param : float
        clipping parameter epsilon
    entcoeff : float
        the entropy loss weight
    optim_epochs : float
        the optimizer's number of epochs
    optim_stepsize : float
        the optimizer's stepsize
    optim_batchsize : int
        the optimizer's the batch size
    gamma : float
        discount factor
    lam : float
        advantage estimation
    adam_epsilon : float
        the epsilon value for the adam optimizer
    schedule : str
        The type of scheduler for the learning rate update ('linear',
        'constant', 'double_linear_con', 'middle_drop' or 'double_middle_drop')
    atarg : tf.placeholder
        Target advantage function (if applicable)
    ret : tf.placeholder
        placeholder for the discounted returns
    lrmult : tf.placeholder
        learning rate multiplier
    action_ph : tf.placeholder
        placeholder for the actions
    old_pi : tf.Tensor
        the output from the policy with trainable parameters from the previous
        step
    losses : TODO
        TODO
    loss_names : list of str
        TODO
    assign_old_eq_new : TODO
        TODO
    optimizer : tf.Operation
        TODO
    """

    def __init__(self,
                 policy,
                 env,
                 gamma=0.99,
                 timesteps_per_batch=2048,
                 clip_param=0.2,
                 entcoeff=0.01,
                 optim_epochs=4,
                 optim_stepsize=3e-4,
                 optim_batchsize=128,
                 lam=0.95,
                 adam_epsilon=1e-5,
                 schedule='constant',
                 verbose=0,
                 policy_kwargs=None):
        """Initialize the algorithm.

        Parameters
        ----------
        env : gym.Env or str
            The environment to learn from (if registered in Gym, can be str)
        policy : TODO
            The policy model to use
        timesteps_per_batch : int
            timesteps per actor per update
        clip_param : float
            clipping parameter epsilon
        entcoeff : float
            the entropy loss weight
        optim_epochs : float
            the optimizer's number of epochs
        optim_stepsize : float
            the optimizer's stepsize
        optim_batchsize : int
            the optimizer's the batch size
        gamma : float
            discount factor
        lam : float
            advantage estimation
        adam_epsilon : float
            the epsilon value for the adam optimizer
        schedule : str
            The type of scheduler for the learning rate update ('linear',
            'constant', 'double_linear_con', 'middle_drop' or
            'double_middle_drop')
        verbose : int
            the verbosity level: 0 none, 1 training information, 2 tensorflow
            debug
        policy_kwargs : dict
            additional arguments to be passed to the policy on creation
        """
        super(PPO, self).__init__(policy, env, timesteps_per_batch, gamma, lam,
                                  verbose, policy_kwargs)

        self.clip_param = clip_param
        self.entcoeff = entcoeff
        self.optim_epochs = optim_epochs
        self.optim_stepsize = optim_stepsize
        self.optim_batchsize = optim_batchsize
        self.adam_epsilon = adam_epsilon
        self.schedule = schedule

        self.atarg = None
        self.ret = None
        self.lrmult = None
        self.action_ph = None
        self.old_pi = None
        self.losses = None
        self.loss_names = None
        self.assign_old_eq_new = None
        self.optimizer = None

        # Perform the algorithm-specific model setup procedure.
        self.setup_model()

        with self.graph.as_default():
            # Create the tensorboard summary.
            self.summary = tf.summary.merge_all()

            # Initialize the model parameters and optimizers.
            with self.sess.as_default():
                self.sess.run(tf.global_variables_initializer())

    def setup_model(self):
        """See parent class."""
        with self.graph.as_default():
            # Create relevant input placeholders.
            with tf.variable_scope("input", reuse=False):
                # Target advantage function (if applicable)
                self.atarg = tf.placeholder(
                    dtype=tf.float32, shape=[None])

                # Empirical return
                self.ret = tf.placeholder(
                    dtype=tf.float32, shape=[None])

                # learning rate multiplier, updated with schedule
                self.lrmult = tf.placeholder(
                    name='lrmult', dtype=tf.float32, shape=[])

                self.action_ph = tf.placeholder(
                    tf.float32, shape=(None, self.action_space.shape[0]))

                tf.summary.scalar('discounted_rewards',
                                  tf.reduce_mean(self.ret))
                tf.summary.scalar('learning_rate',
                                  tf.reduce_mean(self.optim_stepsize))
                tf.summary.scalar('advantage',
                                  tf.reduce_mean(self.atarg))
                tf.summary.scalar('clip_range',
                                  tf.reduce_mean(self.clip_param))

            # Network for old policy
            with tf.variable_scope("oldpi", reuse=False):
                self.old_pi = self.policy(
                    self.sess,
                    self.observation_space,
                    self.action_space,
                    reuse=False,
                    **self.policy_kwargs
                )

            # A utility function that is used to assign the parameters of
            # the new policy to the old policy. Done after every update.
            self.assign_old_eq_new = [
                tf.assign(oldv, newv) for (oldv, newv) in
                zipsame(tf_util.get_globals_vars("oldpi"),
                        tf_util.get_globals_vars("model"))
            ]

            with tf.variable_scope("loss", reuse=False):
                # Annealed clipping parameter epsilon
                clip_param = self.clip_param * self.lrmult

                kloldnew = self.old_pi.proba_distribution.kl(
                    self.policy_pi.proba_distribution)
                ent = self.policy_pi.proba_distribution.entropy()
                meankl = tf.reduce_mean(kloldnew)
                meanent = tf.reduce_mean(ent)
                pol_entpen = -self.entcoeff * meanent

                # pnew / pold
                ratio = tf.exp(
                    self.policy_pi.proba_distribution.logp(self.action_ph) -
                    self.old_pi.proba_distribution.logp(self.action_ph))

                # surrogate from conservative policy iteration
                surr1 = ratio * self.atarg
                surr2 = tf.clip_by_value(
                    ratio, 1.0 - clip_param, 1.0 + clip_param) * self.atarg

                # PPO's pessimistic surrogate (L^CLIP)
                pol_surr = - tf.reduce_mean(tf.minimum(surr1, surr2))
                vf_loss = tf.reduce_mean(
                    tf.square(self.policy_pi.value_flat - self.ret))
                total_loss = pol_surr + pol_entpen + vf_loss
                self.losses = [pol_surr, pol_entpen, vf_loss, meankl, meanent]
                self.loss_names = ["pol_surr", "pol_entpen", "vf_loss", "kl",
                                   "ent"]

                tf.summary.scalar('entropy_loss', pol_entpen)
                tf.summary.scalar('policy_gradient_loss', pol_surr)
                tf.summary.scalar('value_function_loss', vf_loss)
                tf.summary.scalar('approximate_kullback-leibler', meankl)
                tf.summary.scalar('clip_factor', clip_param)
                tf.summary.scalar('loss', total_loss)

            with tf.variable_scope("Adam_mpi", reuse=False):
                # create an optimizer object
                optimizer = tf.compat.v1.train.AdamOptimizer(
                    self.optim_stepsize * self.lrmult)

                # create the optimization operation
                self.optimizer = optimizer.minimize(
                    total_loss,
                    var_list=tf_util.get_trainable_vars("model")
                )

        return tf_util.get_trainable_vars("model")

    def _train(self, seg, writer, total_timesteps):
        """See parent class.

        TODO: describe
        """
        if self.schedule == 'constant':
            cur_lrmult = 1.0
        elif self.schedule == 'linear':
            cur_lrmult = max(
                1.0 - float(self.timesteps_so_far) / total_timesteps, 0)
        else:
            raise NotImplementedError

        observations, actions = seg["observations"], seg["actions"]
        atarg, tdlamret = seg["adv"], seg["tdlamret"]

        # predicted value function before update
        vpredbefore = seg["vpred"]

        # standardized advantage function estimate
        atarg = (atarg - atarg.mean()) / atarg.std()
        dataset = Dataset(dict(ob=observations, ac=actions,
                               atarg=atarg, vtarg=tdlamret),
                          shuffle=True)
        optim_batchsize = self.optim_batchsize or observations.shape[0]

        # set old parameter values to new parameter values
        self.sess.run(self.assign_old_eq_new)

        print("Optimizing...")
        print(fmt_row(13, self.loss_names))

        # Here we do a bunch of optimization epochs over the data.
        for k in range(int(self.optim_epochs)):
            # list of tuples, each of which gives the loss for a minibatch
            losses = []
            for i, batch in enumerate(dataset.iterate_once(optim_batchsize)):
                # TODO: remove?
                steps = (self.timesteps_so_far +
                         k * optim_batchsize +
                         int(i * (optim_batchsize / len(dataset.data_map))))
                # Run loss backprop with summary.
                summary, grad, *newlosses = self.sess.run(
                    [self.summary, self.optimizer] + self.losses,
                    feed_dict={
                        self.policy_pi.obs_ph: batch["ob"],
                        self.old_pi.obs_ph: batch["ob"],
                        self.action_ph: batch["ac"],
                        self.atarg: batch["atarg"],
                        self.ret: batch["vtarg"],
                        self.lrmult: cur_lrmult,
                    }
                )
                writer.add_summary(summary, steps)  # TODO: out of loop?
                losses.append(newlosses)
            print(fmt_row(13, np.mean(losses, axis=0)))

        # TODO: remove?
        print("Evaluating losses...")
        losses = []
        for batch in dataset.iterate_once(optim_batchsize):
            newlosses = self.sess.run(
                self.losses,
                feed_dict={
                    self.policy_pi.obs_ph: batch["ob"],
                    self.old_pi.obs_ph: batch["ob"],
                    self.action_ph: batch["ac"],
                    self.atarg: batch["atarg"],
                    self.ret: batch["vtarg"],
                    self.lrmult: cur_lrmult,
                }
            )
            losses.append(newlosses)
        mean_losses, _, _ = mpi_moments(losses, axis=0)

        return mean_losses, vpredbefore, tdlamret
