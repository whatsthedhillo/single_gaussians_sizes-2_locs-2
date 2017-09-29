from __future__ import division
from __future__ import absolute_import
from __future__ import print_function

import numpy as np
import tensorflow as tf
from IPython import embed
import sys

# Requires Python 3.6+ and Tensorflow 1.1+

class VaDE():


    def __init__(self, input_shape, encoder, latent_dim, decoder, hyperParams):

        self.input_dim = input_shape
        self.num_input_vals = np.prod(input_shape)
        self.encoder = encoder
        self.latent_dim = latent_dim
        self.decoder = decoder
        self.batch_size = hyperParams['batch_size'] # add error checking
        self.learning_rate = hyperParams['learning_rate'] # Add error checking
        if hyperParams['reconstruct_cost'] in ['bernoulli', 'gaussian']:
            self.reconstruct_cost = hyperParams['reconstruct_cost'] # Add error checking
        else:
            SystemExit("ERR: Only Gaussian and Bernoulli Reconstruction Functionality\n")
        self.optimizer = hyperParams['optimizer'] # Add error checking
        self.num_clusters = hyperParams['num_clusters']
        self.alpha = hyperParams['alpha']

        self.__build_graph()
        self.__create_loss()

        # Launch the session
        config = tf.ConfigProto()
        config.gpu_options.allow_growth=True
        self.sess = tf.InteractiveSession(config=config)
        self.sess.run(tf.global_variables_initializer())

        self.saver = tf.train.Saver()


    def __call__(self, network_input):

        targets = (self.cost, self.reconstruct_loss, self.regularizer, self.train_op)
        input_dict = {self.network_input: network_input}
        cost, reconstruct_loss, regularizer, _ = self.sess.run(targets, feed_dict=input_dict)
        # Normalize the modes after optimization step
        self.sess.run(self.normalize_pis_op)

        return (cost, reconstruct_loss, regularizer)


    def __build_graph(self):


        # These values are not output from a network. They are variables
        # in the cost function. As a consequence they are learned during
        # the optimization procedure. So essentially, the network architecture
        # or framework is not different than a traditional VAE. Here we just
        # add extra variables and then learn them in the modified cost function
        pi_init = np.ones(self.num_clusters)/self.num_clusters
        self.gmm_pi = tf.Variable(pi_init, dtype=tf.float32)

        means = np.zeros(self.latent_dim)
        cov = np.eye(self.latent_dim)
        mu_init = np.random.multivariate_normal(means, cov, self.num_clusters)
        #mu_init = np.zeros((self.latent_dim,self.num_clusters))
        self.gmm_mu = tf.Variable(mu_init.T, dtype=tf.float32)

        log_var_init = np.ones((self.latent_dim, self.num_clusters))
        self.gmm_log_var = tf.Variable(log_var_init, dtype=tf.float32)

        self.network_input = tf.placeholder(tf.float32, name="network_input")

        # Construct the encoder network and get its output
        encoder_output = self.encoder.build_graph(self.network_input, self.input_dim)
        #enc_output_dim = encoder_output.shape.as_list()[1]
        enc_output_dim = self.encoder.get_output_dim()

        # Now add the weights/bias for the mean and var of the latency dim
        z_mean_weight_val = self.encoder.xavier_init((enc_output_dim, self.latent_dim))
        z_mean_weight = tf.Variable(initial_value=z_mean_weight_val, dtype=tf.float32)
        z_mean_bias_val = np.zeros((1,self.latent_dim))
        z_mean_bias = tf.Variable(initial_value=z_mean_bias_val, dtype=tf.float32)

        self.z_mean = encoder_output @ z_mean_weight + z_mean_bias

        z_log_var_weight_val = self.encoder.xavier_init((enc_output_dim, self.latent_dim))
        z_log_var_weight = tf.Variable(initial_value=z_log_var_weight_val, dtype=tf.float32)
        z_log_var_bias_val = np.zeros((1,self.latent_dim))
        z_log_var_bias = tf.Variable(initial_value=z_log_var_bias_val, dtype=tf.float32)

        self.z_log_var = encoder_output @ z_log_var_weight + z_log_var_bias

        z_shape = tf.shape(self.z_log_var)
        eps = tf.random_normal(z_shape, dtype=tf.float32)
        self.z = self.z_mean + tf.sqrt(tf.exp(self.z_log_var)) * eps

        # Construct the decoder network and get its output
        decoder_output = self.decoder.build_graph(self.z, self.latent_dim)
        #dec_output_dim = decoder_output.shape.as_list()[1]
        dec_output_dim = self.decoder.get_output_dim()

        # Now add the weights/bias for the mean reconstruction terms
        x_mean_weight_val = self.decoder.xavier_init((dec_output_dim, self.num_input_vals))
        x_mean_weight = tf.Variable(initial_value=x_mean_weight_val, dtype=tf.float32)
        x_mean_bias_val = np.zeros((1,self.num_input_vals))
        x_mean_bias = tf.Variable(initial_value=x_mean_bias_val, dtype=tf.float32)

        # Just do Bernoulli for now. Add more functionality later
        if self.reconstruct_cost == 'bernoulli':
            self.x_mean = tf.nn.sigmoid(decoder_output @ x_mean_weight + x_mean_bias)
        elif self.reconstruct_cost == 'gaussian':
            self.x_mean = decoder_output @ x_mean_weight + x_mean_bias
            # Now add the weights/bias for the sigma reconstruction term
            x_sigma_weight_val = self.encoder.xavier_init((dec_output_dim,
                self.num_input_vals))
            x_sigma_weight = tf.Variable(initial_value=x_sigma_weight_val, dtype=tf.float32)
            x_sigma_bias_val = np.zeros(self.num_input_vals)
            x_sigma_bias = tf.Variable(initial_value=x_mean_bias_val, dtype=tf.float32)
            self.x_sigma = decoder_output @ x_sigma_weight + x_sigma_bias


    def __create_loss(self):

        # Reshape the GMM tensors in a frustratingly convoluted way to
        # be able to vectorize the computation of p(z|x) = E[p(c|z)]
        gmm_pi = tf.reshape(self.gmm_pi, (1,self.num_clusters))
        gmm_mu = tf.reshape(tf.tile(self.gmm_mu, [self.batch_size,1]),
                (self.batch_size,self.latent_dim,self.num_clusters))
        gmm_log_var = tf.reshape(tf.tile(self.gmm_log_var, [self.batch_size,1]),
                (self.batch_size,self.latent_dim,self.num_clusters))
        z = tf.reshape(self.z, (self.batch_size, self.latent_dim, 1))

        # First calculate the numerator p(c,z) = p(c)p(z|c) (vectorized)
        # resulting shape = (batch_size, num_clusters)
        p_c_z = tf.exp(tf.log(gmm_pi) - 0.5*(tf.log(2*np.pi) +
                tf.reduce_sum(gmm_log_var + tf.square(z-gmm_mu) /
                tf.exp(gmm_log_var), axis=1))) + 1e-10

        # Next we sum over the clusters making the marginal probability p(z)
        marginal = tf.reduce_sum(p_c_z, axis=1, keep_dims=True)

        # Finally we calculate the resulting posterior p(c|z), in GMM clustering
        # literature this is called the 'responsibility' and is denoted by a
        # gamma - shape = (batch_size, num_clusters)
        gamma = p_c_z / marginal

        if self.reconstruct_cost == "bernoulli":
            # log p(x|z) - shape=(batchsize)
            p_x_z = tf.reduce_sum(self.network_input * tf.log(1e-10 + self.x_mean)
                               + (1-self.network_input) * tf.log(1e-10 + 1 -
                                   self.x_mean), axis=1)
        elif self.reconstruct_cost == "gaussian":
            # log p(x|z) - shape=(batchsize)
            p_x_z = tf.reduce_sum(tf.square(tf.subtract(self.network_input,
                self.x_mean)), axis=1)

        # log q(z|x) - shape=(batch_size)
        q_z_x = -0.5*(self.latent_dim*tf.log(2*np.pi) +
                 tf.reduce_sum(self.z_log_var + tf.square(self.z-self.z_mean)
                     / tf.exp(self.z_log_var), axis=1))

        # log p(z|c) - shape=(batch_size,num_clusters)
        p_z_c = -0.5*(tf.log(2*np.pi) + tf.reduce_sum(gmm_log_var +
                tf.square(z-gmm_mu)/tf.exp(gmm_log_var),
                axis=1)) + 1e-10

        # log p(c) - shape=(num_clusters)
        p_c = tf.log(tf.reshape(gmm_pi, [self.num_clusters]))

        # log q(c|x) = log E[p(c|z)] - shape=(num_clusters)
        q_c_x = tf.log(tf.reduce_mean(gamma,axis=0))

        self.cost = -(tf.reduce_mean(p_x_z-q_z_x) +
                    tf.reduce_sum(tf.exp(q_c_x) *
                    (tf.reduce_mean(p_z_c,axis=0) +
                    p_c - q_c_x)))

        self.reconstruct_loss = tf.reduce_mean(p_x_z)
        self.regularizer = self.cost - self.reconstruct_loss

        # User specifies optimizer in the hyperParams argument to constructor
        self.train_op = self.optimizer(learning_rate=self.learning_rate).minimize(self.cost)

        # Ensure modes are normalized
        self.normalize_pis_op = tf.assign(self.gmm_pi,self.gmm_pi/tf.reduce_sum(self.gmm_pi))


    def reconstruct(self, network_input):

        if self.reconstruct_cost == 'bernoulli':
            return self.sess.run(self.x_mean, feed_dict={self.network_input: network_input})
        elif self.reconstruct_cost == 'gaussian':
            input_dict = {self.network_input: network_input}
            targets = (self.x_mean, self.x_sigma)
            mean, sig = self.sess.run(targets, feed_dict=input_dict)
            eps = tf.random_normal(tf.shape(sigma), dtype=tf.float32)
            return mean + sigma * eps


    def transform(self, network_input):

        input_dict={self.network_input: network_input}
        targets = (self.z_mean, self.z_log_var)
        means, log_vars = self.sess.run(targets, feed_dict=input_dict)
        return (means, np.sqrt(np.exp(log_vars)))


    def generate(self, z=None):

        if z is None:
            targets = (self.gmm_pi, self.gmm_mu, self.gmm_log_var)
            pis, means, log_vars = self.sess.run(targets)
            embed()
            sys.exit()
            cluster = np.random.choice(range(self.num_clusters), p=pis)
            mean = means[cluster]
            std = np.sqrt(np.exp(log_vars[cluster]))
            eps = np.random_normal(std.shape)
            z = mean + std * eps
            return self.sess.run(self.x_mean, feed_dict={self.z: z})
        else:
            return self.sess.run(self.x_mean, feed_dict={self.z: z})


    def get_gmm_params(self):

        targets = (self.gmm_pi, self.gmm_mu, self.gmm_log_var)
        pis, means, log_vars = self.sess.run(targets)
        return (pis, means, np.sqrt(np.exp(log_vars)))
