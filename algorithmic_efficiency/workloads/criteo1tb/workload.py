import math
from typing import Dict, Optional

from absl import flags
import jax

from algorithmic_efficiency import spec
from algorithmic_efficiency.pytorch_utils import pytorch_setup
from algorithmic_efficiency.workloads.criteo1tb import input_pipeline
from algorithmic_efficiency.workloads.criteo1tb.criteo1tb_jax import metrics

FLAGS = flags.FLAGS


_NUM_DENSE_FEATURES = 13
_VOCAB_SIZES = tuple([1024 * 128] * 26)


class BaseCriteo1TbDlrmSmallWorkload(spec.Workload):
  """Criteo1tb workload."""
  def __init__(self):
    self._eval_iters = {}
    self._param_shapes = None
    self._param_types = None

  def has_reached_goal(self, eval_result: float) -> bool:
    return eval_result['validation/loss'] < self.target_value

  @property
  def target_value(self):
    return 0.12

  @property
  def loss_type(self):
    return spec.LossType.SIGMOID_CROSS_ENTROPY

  @property
  def num_train_examples(self):
    return 4_195_197_692

  @property
  def num_eval_train_examples(self):
    return 100_000

  @property
  def num_validation_examples(self):
    return 131072 * 8  # TODO(znado): finalize the validation split size.

  @property
  def num_test_examples(self):
    return None

  @property
  def train_mean(self):
    return 0.0

  @property
  def train_stddev(self):
    return 1.0

  @property
  def max_allowed_runtime_sec(self):
    return 6 * 60 * 60

  @property
  def eval_period_time_sec(self):
    return 20 * 60

  def output_activation_fn(self,
                           logits_batch: spec.Tensor,
                           loss_type: spec.LossType) -> spec.Tensor:
    """Return the final activations of the model."""
    pass

  def build_input_queue(self,
                        data_rng: jax.random.PRNGKey,
                        split: str,
                        data_dir: str,
                        global_batch_size: int,
                        num_batches: Optional[int] = None,
                        repeat_final_dataset: bool = False):
    del data_rng
    ds = input_pipeline.get_criteo1tb_dataset(
        split=split,
        data_dir=data_dir,
        is_training=(split == 'train'),
        global_batch_size=global_batch_size,
        num_dense_features=_NUM_DENSE_FEATURES,
        vocab_sizes=_VOCAB_SIZES,
        num_batches=num_batches,
        repeat_final_dataset=repeat_final_dataset)
    for batch in iter(ds):
      batch = jax.tree_map(lambda x: x._numpy(), batch)  # pylint: disable=protected-access
      yield batch

  # Return whether or not a key in spec.ParameterContainer is the output layer
  # parameters.
  def is_output_params(self, param_key: spec.ParameterKey) -> bool:
    pass

  @property
  def param_shapes(self):
    """The shapes of the parameters in the workload model."""
    if self._param_shapes is None:
      raise ValueError(
          'This should not happen, workload.init_model_fn() should be called '
          'before workload.param_shapes!')
    return self._param_shapes

  def _eval_metric(self, labels, logits, weights):
    per_example_losses = metrics.per_example_sigmoid_binary_cross_entropy(
        logits, labels)
    return jax.lax.psum(per_example_losses), jax.lax.psum(weights)

  def _eval_batch(self, params, batch, model_state, rng):
    logits, _ = self.model_fn(
        params,
        batch,
        model_state,
        spec.ForwardPassMode.EVAL,
        rng,
        update_batch_norm=False)
    return self._eval_metric(batch['targets'], logits, batch['weights'])

  def _eval_model_on_split(self,
                           split: str,
                           num_examples: int,
                           global_batch_size: int,
                           params: spec.ParameterContainer,
                           model_state: spec.ModelAuxiliaryState,
                           rng: spec.RandomState,
                           data_dir: str,
                           global_step: int = 0) -> Dict[str, float]:
    """Run a full evaluation of the model."""
    del model_state
    num_batches = int(math.ceil(num_examples / global_batch_size))
    if split not in self._eval_iters:
      # These iterators will repeat indefinitely.
      self._eval_iters[split] = self.build_input_queue(
          rng,
          split,
          data_dir,
          global_batch_size,
          num_batches,
          repeat_final_dataset=True)
    total_loss_numerator = 0.
    total_loss_denominator = 0.
    for _ in range(num_batches):
      eval_batch = next(self._eval_iters[split])
      batch_loss_numerator, batch_loss_denominator = (
          self.eval_step_pmapped(params, eval_batch).unreplicate())
      total_loss_numerator += batch_loss_numerator
      total_loss_denominator += batch_loss_denominator
    mean_loss = total_loss_numerator / total_loss_denominator
    return mean_loss.numpy()
