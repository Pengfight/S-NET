import tensorflow as tf
from func import cudnn_gru, native_gru, dot_attention, summ, dropout, ptr_net


class Model(object):
	def __init__(self, config, batch, word_mat=None, char_mat=None, trainable=True, opt=True):
		self.config = config
		self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32,
										   initializer=tf.constant_initializer(0), trainable=False)
		self.c, self.q, self.ch, self.qh, self.y1, self.y2, self.qa_id = batch.get_next()
		self.is_train = tf.get_variable(
			"is_train", shape=[], dtype=tf.bool, trainable=False)
		self.word_mat = tf.get_variable("word_mat", initializer=tf.constant(
			word_mat, dtype=tf.float32), trainable=False)
		self.char_mat = tf.get_variable(
			"char_mat", char_mat.shape, dtype=tf.float32)

		self.c_mask = tf.cast(self.c, tf.bool)
		self.q_mask = tf.cast(self.q, tf.bool)
		self.c_len = tf.reduce_sum(tf.cast(self.c_mask, tf.int32), axis=1)
		self.q_len = tf.reduce_sum(tf.cast(self.q_mask, tf.int32), axis=1)

		if opt:
			N, CL = config.batch_size, config.char_limit
			self.c_maxlen = tf.reduce_max(self.c_len)
			self.q_maxlen = tf.reduce_max(self.q_len)
			self.c = tf.slice(self.c, [0, 0], [N, self.c_maxlen])
			self.q = tf.slice(self.q, [0, 0], [N, self.q_maxlen])
			self.c_mask = tf.slice(self.c_mask, [0, 0], [N, self.c_maxlen])
			self.q_mask = tf.slice(self.q_mask, [0, 0], [N, self.q_maxlen])
			self.ch = tf.slice(self.ch, [0, 0, 0], [N, self.c_maxlen, CL])
			self.qh = tf.slice(self.qh, [0, 0, 0], [N, self.q_maxlen, CL])
			self.y1 = tf.slice(self.y1, [0, 0], [N, self.c_maxlen])
			self.y2 = tf.slice(self.y2, [0, 0], [N, self.c_maxlen])
		else:
			self.c_maxlen, self.q_maxlen = config.para_limit, config.ques_limit

		self.ch_len = tf.reshape(tf.reduce_sum(
			tf.cast(tf.cast(self.ch, tf.bool), tf.int32), axis=2), [-1])
		self.qh_len = tf.reshape(tf.reduce_sum(
			tf.cast(tf.cast(self.qh, tf.bool), tf.int32), axis=2), [-1])

		self.ready()

		if trainable:
			self.lr = tf.get_variable(
				"lr", shape=[], dtype=tf.float32, trainable=False)
			self.opt = tf.train.AdadeltaOptimizer(
				learning_rate=self.lr, epsilon=1e-6)
			grads = self.opt.compute_gradients(self.loss)
			gradients, variables = zip(*grads)
			capped_grads, _ = tf.clip_by_global_norm(
				gradients, config.grad_clip)
			self.train_op = self.opt.apply_gradients(
				zip(capped_grads, variables), global_step=self.global_step)

	def ready(self):
		config = self.config
		N, PL, QL, CL, d, dc, dg = config.batch_size, self.c_maxlen, self.q_maxlen, config.char_limit, config.hidden, config.char_dim, config.char_hidden
		gru = cudnn_gru if config.use_cudnn else native_gru

		with tf.variable_scope("emb"):
			with tf.variable_scope("char"):
				ch_emb = tf.reshape(tf.nn.embedding_lookup(
					self.char_mat, self.ch), [N * PL, CL, dc])
				qh_emb = tf.reshape(tf.nn.embedding_lookup(
					self.char_mat, self.qh), [N * QL, CL, dc])
				ch_emb = dropout(
					ch_emb, keep_prob=config.keep_prob, is_train=self.is_train)
				qh_emb = dropout(
					qh_emb, keep_prob=config.keep_prob, is_train=self.is_train)
				cell_fw = tf.contrib.rnn.GRUCell(dg)
				cell_bw = tf.contrib.rnn.GRUCell(dg)
				_, (state_fw, state_bw) = tf.nn.bidirectional_dynamic_rnn(
					cell_fw, cell_bw, ch_emb, self.ch_len, dtype=tf.float32)
				ch_emb = tf.concat([state_fw, state_bw], axis=1)
				_, (state_fw, state_bw) = tf.nn.bidirectional_dynamic_rnn(
					cell_fw, cell_bw, qh_emb, self.qh_len, dtype=tf.float32)
				qh_emb = tf.concat([state_fw, state_bw], axis=1)
				qh_emb = tf.reshape(qh_emb, [N, QL, 2 * dg])
				ch_emb = tf.reshape(ch_emb, [N, PL, 2 * dg])

			with tf.name_scope("word"):
				c_emb = tf.nn.embedding_lookup(self.word_mat, self.c)
				q_emb = tf.nn.embedding_lookup(self.word_mat, self.q)

			c_emb = tf.concat([c_emb, ch_emb], axis=2)
			q_emb = tf.concat([q_emb, qh_emb], axis=2)

		with tf.variable_scope("encoding"):
			rnn = gru(num_layers=3, num_units=d, batch_size=N, input_size=c_emb.get_shape(
			).as_list()[-1], keep_prob=config.keep_prob, is_train=self.is_train)
			c = rnn(c_emb, seq_len=self.c_len)
			q = rnn(q_emb, seq_len=self.q_len)

		with tf.variable_scope("attention"):
			qc_att = dot_attention(c, q, mask=self.q_mask, hidden=d,
								   keep_prob=config.keep_prob, is_train=self.is_train)
			rnn = gru(num_layers=1, num_units=d, batch_size=N, input_size=qc_att.get_shape(
			).as_list()[-1], keep_prob=config.keep_prob, is_train=self.is_train)
			att = rnn(qc_att, seq_len=self.c_len)

		with tf.variable_scope("match"):
			self_att = dot_attention(
				att, att, mask=self.c_mask, hidden=d, keep_prob=config.keep_prob, is_train=self.is_train)
			rnn = gru(num_layers=1, num_units=d, batch_size=N, input_size=self_att.get_shape(
			).as_list()[-1], keep_prob=config.keep_prob, is_train=self.is_train)
			match = rnn(self_att, seq_len=self.c_len)

		with tf.variable_scope("pointer"):

			# r_Q:
			init = summ(q[:, :, -2 * d:], d, mask=self.q_mask,
						keep_prob=config.ptr_keep_prob, is_train=self.is_train)
			
			pointer = ptr_net(batch=N, hidden=init.get_shape().as_list(
			)[-1], keep_prob=config.ptr_keep_prob, is_train=self.is_train)
			logits1, logits2 = pointer(init, match, d, self.c_mask)

		with tf.variable_scope("predict"):
			outer = tf.matmul(tf.expand_dims(tf.nn.softmax(logits1), axis=2),
							  tf.expand_dims(tf.nn.softmax(logits2), axis=1))
			outer = tf.matrix_band_part(outer, 0, 15)
			self.yp1 = tf.argmax(tf.reduce_max(outer, axis=2), axis=1)
			self.yp2 = tf.argmax(tf.reduce_max(outer, axis=1), axis=1)
			losses = tf.nn.softmax_cross_entropy_with_logits(
				logits=logits1, labels=self.y1)
			losses2 = tf.nn.softmax_cross_entropy_with_logits(
				logits=logits2, labels=self.y2)
			self.loss = tf.reduce_mean(losses + losses2)

			"""
			# Create a summary operation
			summary_op1 = tf.summary.tensor_summary('softmax_input', out)
			summary_op2 = tf.summary.tensor_summary('softmax_input', out)
			summary_op3 = tf.summary.tensor_summary('softmax_input', out)
			summary_op4 = tf.summary.tensor_summary('softmax_input', out)

			# Create the summary
			summary_str = sess.run(summary_op)

			# Create a summary writer
			writer = tf.train.SummaryWriter(...)

			# Write the summary
			writer.add_summary(summary_str)
			# print losses
			condition = tf.greater(self.loss, 11)
			self.yp1 = tf.where(condition, tf.Print(self.yp1,[self.yp1],message="Yp1:"), self.yp1)
			self.yp2 = tf.where(condition, tf.Print(self.yp2,[self.yp2],message="Yp2:"), self.yp1)
			"""
	def variable_summaries(var):
		"""Attach a lot of summaries to a Tensor (for TensorBoard visualization)."""
		with tf.name_scope('summaries'):
			mean = tf.reduce_mean(var)
			tf.summary.scalar('mean', mean)
			with tf.name_scope('stddev'):
				stddev = tf.sqrt(tf.reduce_mean(tf.square(var - mean)))
			tf.summary.scalar('stddev', stddev)
			tf.summary.scalar('max', tf.reduce_max(var))
			tf.summary.scalar('min', tf.reduce_min(var))
			tf.summary.histogram('histogram', var)

	def print(self):
		pass

	def get_loss(self):
		return self.loss

	def get_global_step(self):
		return self.global_step
