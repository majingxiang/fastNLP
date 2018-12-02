
import warnings
import inspect
from collections import defaultdict

import numpy as np
import torch

from fastNLP.core.utils import get_func_signature
from fastNLP.core.utils import _check_arg_dict_list
from fastNLP.core.utils import _build_args
from fastNLP.core.utils import CheckError
from fastNLP.core.utils import seq_lens_to_masks

class MetricBase(object):
    def __init__(self):
        self.param_map = {} # key is param in function, value is input param.
        self._checked = False

    def evaluate(self, *args, **kwargs):
        raise NotImplementedError

    def _init_param_map(self, key_map=None, **kwargs):
        """

        check the validity of key_map and other param map. Add these into self.param_map
        :param key_map: dict
        :param kwargs:
        :return: None
        """
        value_counter = defaultdict(set)
        if key_map is not None:
            if not isinstance(key_map, dict):
                raise TypeError("key_map must be `dict`, got {}.".format(type(key_map)))
            for key, value in key_map.items():
                if value is None:
                    self.param_map[key] = key
                    continue
                if isinstance(key, str):
                    raise TypeError(f"key in key_map must be `str`, not `{type(key)}`.")
                if isinstance(value, str):
                    raise TypeError(f"value in key_map must be `str`, not `{type(value)}`.")
                self.param_map[key] = value
                value_counter[value].add(key)
        for key, value in kwargs.items():
            if value is None:
                self.param_map[key] = key
                continue
            if not isinstance(value, str):
                raise TypeError(f"in {key}={value}, value must be `str`, not `{type(value)}`.")
            self.param_map[key] = value
            value_counter[value].add(key)
        for value, key_set in value_counter.items():
            if len(key_set)>1:
                raise ValueError(f"Several parameters:{key_set} are provided with one output {value}.")

        # check consistence between signature and param_map
        func_spect = inspect.getfullargspec(self.evaluate)
        func_args = [arg for arg in func_spect.args if arg!='self']
        for func_param, input_param in self.param_map.items():
            if func_param not in func_args:
                raise NameError(f"Parameter `{func_param}` is not in {get_func_signature(self.evaluate)}. Please check the "
                                f"initialization parameters, or change the signature of"
                                f" {get_func_signature(self.evaluate)}.")

        # evaluate should not have varargs.
        if func_spect.varargs:
            raise NameError(f"Delete `*{func_spect.varargs}` in {get_func_signature(self.evaluate)}(Do not use "
                            f"positional argument.).")

    def get_metric(self, reset=True):
        raise NotImplemented

    def __call__(self, pred_dict, target_dict, check=False):
        """

        This method will call self.evaluate method.
        Before calling self.evaluate, it will first check the validity ofoutput_dict, target_dict
            (1) whether self.evaluate has varargs, which is not supported.
            (2) whether params needed by self.evaluate is not included in output_dict,target_dict.
            (3) whether params needed by self.evaluate duplicate in output_dict, target_dict
            (4) whether params in output_dict, target_dict are not used by evaluate.(Might cause warning)
        Besides, before passing params into self.evaluate, this function will filter out params from output_dict and
            target_dict which are not used in self.evaluate. (but if **kwargs presented in self.evaluate, no filtering
            will be conducted)
        :param pred_dict: usually the output of forward or prediction function
        :param target_dict: usually features set as target..
        :param check: boolean, if check is True, it will force check `varargs, missing, unsed, duplicated`.
        :return:
        """
        if not callable(self.evaluate):
            raise TypeError(f"{self.__class__.__name__}.evaluate has to be callable, not {type(self.evaluate)}.")

        if not self._checked:
            # 1. check consistence between signature and param_map
            func_spect = inspect.getfullargspec(self.evaluate)
            func_args = set([arg for arg in func_spect.args if arg!='self'])
            for func_arg, input_arg in self.param_map.items():
                if func_arg not in func_args:
                    raise NameError(f"`{func_arg}` not in {get_func_signature(self.evaluate)}.")

            # 2. only part of the param_map are passed, left are not
            for arg in func_args:
                if arg not in self.param_map:
                    self.param_map[arg] = arg #This param does not need mapping.
            self._evaluate_args = func_args
            self._reverse_param_map = {input_arg: func_arg for func_arg, input_arg in self.param_map.items()}

        # need to wrap inputs in dict.
        mapped_pred_dict = {}
        mapped_target_dict = {}
        for input_arg in set(list(pred_dict.keys()) + list(target_dict.keys())):
            if input_arg in self._reverse_param_map:
                mapped_arg = self._reverse_param_map[input_arg]
            else:
                mapped_arg = input_arg
            if input_arg in pred_dict:
                mapped_pred_dict[mapped_arg] = pred_dict[input_arg]
            if input_arg in target_dict:
                mapped_target_dict[mapped_arg] = target_dict[input_arg]

        # check duplicated, unused, missing
        if check or not self._checked:
            check_res = _check_arg_dict_list(self.evaluate, [mapped_pred_dict, mapped_target_dict])
            for key in check_res._fields:
                value = getattr(check_res, key)
                new_value = list(value)
                # TODO 这里报错的逻辑应该是怎样的？
                for idx, func_arg in enumerate(value):
                    if func_arg in self.param_map:
                        new_value[idx] = self.param_map[func_arg] + f'(try to get value from {self.param_map[func_arg]})'
                    else:
                        new_value[idx] = func_arg
            if check_res.missing or check_res.duplicated or check_res.varargs:
                raise CheckError(check_res=check_res,
                                 func_signature=get_func_signature(self.evaluate))
        refined_args = _build_args(self.evaluate, **mapped_pred_dict, **mapped_target_dict)

        self.evaluate(**refined_args)
        self._checked = True


class AccuracyMetric(MetricBase):
    def __init__(self, pred=None, target=None, masks=None, seq_lens=None):
        super().__init__()

        self._init_param_map(pred=pred, target=target,
                             masks=masks, seq_lens=seq_lens)

        self.total = 0
        self.acc_count = 0

    def evaluate(self, pred, target, masks=None, seq_lens=None):
        """

        :param pred: List of (torch.Tensor, or numpy.ndarray). Element's shape can be:
                torch.Size([B,]), torch.Size([B, n_classes]), torch.Size([B, max_len]), torch.Size([B, max_len, n_classes])
        :param target: List of (torch.Tensor, or numpy.ndarray). Element's can be:
                torch.Size([B,]), torch.Size([B,]), torch.Size([B, max_len]), torch.Size([B, max_len])
        :param masks: List of (torch.Tensor, or numpy.ndarray). Element's can be:
                None, None, torch.Size([B, max_len], torch.Size([B, max_len])
        :param seq_lens: List of (torch.Tensor, or numpy.ndarray). Element's can be:
                None, None, torch.Size([B], torch.Size([B]). ignored if masks are provided.
        :return: dict({'acc': float})
        """
        if not isinstance(pred, torch.Tensor):
            raise TypeError(f"`pred` in {get_func_signature(self.evaluate)} must be torch.Tensor,"
                            f"got {type(pred)}.")
        if not isinstance(target, torch.Tensor):
            raise TypeError(f"`target` in {get_func_signature(self.evaluate)} must be torch.Tensor,"
                            f"got {type(target)}.")

        if masks is not None and not isinstance(masks, torch.Tensor):
            raise TypeError(f"`masks` in {get_func_signature(self.evaluate)} must be torch.Tensor,"
                            f"got {type(masks)}.")
        elif seq_lens is not None and not isinstance(seq_lens, torch.Tensor):
            raise TypeError(f"`seq_lens` in {get_func_signature(self.evaluate)} must be torch.Tensor,"
                                f"got {type(seq_lens)}.")

        if masks is None and seq_lens is not None:
            masks = seq_lens_to_masks(seq_lens=seq_lens, float=True)

        if pred.size()==target.size():
            pass
        elif len(pred.size())==len(target.size())+1:
            pred = pred.argmax(dim=-1)
        else:
            raise RuntimeError(f"In {get_func_signature(self.evaluate)}, when pred have "
                               f"size:{pred.size()}, target should have size: {pred.size()} or "
                               f"{pred.size()[:-1]}, got {target.size()}.")

        pred = pred.float()
        target = target.float()

        if masks is not None:
            self.acc_count += torch.sum(torch.eq(pred, target).float() * masks.float()).item()
            self.total += torch.sum(masks.float()).item()
        else:
            self.acc_count +=  torch.sum(torch.eq(pred, target).float()).item()
            self.total += np.prod(list(pred.size()))

    def get_metric(self, reset=True):
        evaluate_result = {'acc': self.acc_count/self.total}
        if reset:
            self.acc_count = 0
            self.total = 0
        return evaluate_result



def _prepare_metrics(metrics):
    """

    Prepare list of Metric based on input
    :param metrics:
    :return: List[fastNLP.MetricBase]
    """
    _metrics = []
    if metrics:
        if isinstance(metrics, list):
            for metric in metrics:
                if isinstance(metric, type):
                    metric = metric()
                if isinstance(metric, MetricBase):
                    metric_name = metric.__class__.__name__
                    if not callable(metric.evaluate):
                        raise TypeError(f"{metric_name}.evaluate must be callable, got {type(metric.evaluate)}.")
                    if not callable(metric.get_metric):
                        raise TypeError(f"{metric_name}.get_metric must be callable, got {type(metric.get_metric)}.")
                    _metrics.append(metric)
                else:
                    raise TypeError(f"The type of metric in metrics must be `fastNLP.MetricBase`, not `{type(metric)}`.")
        elif isinstance(metrics, MetricBase):
            _metrics = [metrics]
        else:
            raise TypeError(f"The type of metrics should be `list[fastNLP.MetricBase]` or `fastNLP.MetricBase`, "
                            f"got {type(metrics)}.")
    return _metrics


class Evaluator(object):
    def __init__(self):
        pass

    def __call__(self, predict, truth):
        """

        :param predict: list of tensors, the network outputs from all batches.
        :param truth: list of dict, the ground truths from all batch_y.
        :return:
        """
        raise NotImplementedError

class ClassifyEvaluator(Evaluator):
    def __init__(self):
        super(ClassifyEvaluator, self).__init__()

    def __call__(self, predict, truth):
        y_prob = [torch.nn.functional.softmax(y_logit, dim=-1) for y_logit in predict]
        y_prob = torch.cat(y_prob, dim=0)
        y_pred = torch.argmax(y_prob, dim=-1)
        y_true = torch.cat(truth, dim=0)
        acc = float(torch.sum(y_pred == y_true)) / len(y_true)
        return {"accuracy": acc}


class SeqLabelEvaluator(Evaluator):
    def __init__(self):
        super(SeqLabelEvaluator, self).__init__()

    def __call__(self, predict, truth, **_):
        """

        :param predict: list of List, the network outputs from all batches.
        :param truth: list of dict, the ground truths from all batch_y.
        :return accuracy:
        """
        total_correct, total_count = 0., 0.
        for x, y in zip(predict, truth):
            x = torch.tensor(x)
            y = y.to(x)  # make sure they are in the same device
            mask = (y > 0)
            correct = torch.sum(((x == y) * mask).long())
            total_correct += float(correct)
            total_count += float(torch.sum(mask.long()))
        accuracy = total_correct / total_count
        return {"accuracy": float(accuracy)}

class SeqLabelEvaluator2(Evaluator):
    # 上面的evaluator应该是错误的
    def __init__(self, seq_lens_field_name='word_seq_origin_len'):
        super(SeqLabelEvaluator2, self).__init__()
        self.end_tagidx_set = set()
        self.seq_lens_field_name = seq_lens_field_name

    def __call__(self, predict, truth, **_):
        """

        :param predict: list of batch, the network outputs from all batches.
        :param truth: list of dict, the ground truths from all batch_y.
        :return accuracy:
        """
        seq_lens = _[self.seq_lens_field_name]
        corr_count = 0
        pred_count = 0
        truth_count = 0
        for x, y, seq_len in zip(predict, truth, seq_lens):
            x = x.cpu().numpy()
            y = y.cpu().numpy()
            for idx, s_l in enumerate(seq_len):
                x_ = x[idx]
                y_ = y[idx]
                x_ = x_[:s_l]
                y_ = y_[:s_l]
                flag = True
                start = 0
                for idx_i, (x_i, y_i) in enumerate(zip(x_, y_)):
                    if x_i in self.end_tagidx_set:
                        truth_count += 1
                        for j in range(start, idx_i + 1):
                            if y_[j]!=x_[j]:
                                flag = False
                                break
                        if flag:
                            corr_count += 1
                        flag = True
                        start = idx_i + 1
                    if y_i in self.end_tagidx_set:
                        pred_count += 1
        P = corr_count / (float(pred_count) + 1e-6)
        R = corr_count / (float(truth_count) + 1e-6)
        F = 2 * P * R / (P + R + 1e-6)

        return {"P": P, 'R':R, 'F': F}



class SNLIEvaluator(Evaluator):
    def __init__(self):
        super(SNLIEvaluator, self).__init__()

    def __call__(self, predict, truth):
        y_prob = [torch.nn.functional.softmax(y_logit, dim=-1) for y_logit in predict]
        y_prob = torch.cat(y_prob, dim=0)
        y_pred = torch.argmax(y_prob, dim=-1)
        truth = [t['truth'] for t in truth]
        y_true = torch.cat(truth, dim=0).view(-1)
        acc = float(torch.sum(y_pred == y_true)) / y_true.size(0)
        return {"accuracy": acc}


def _conver_numpy(x):
    """convert input data to numpy array

    """
    if isinstance(x, np.ndarray):
        return x
    elif isinstance(x, torch.Tensor):
        return x.numpy()
    elif isinstance(x, list):
        return np.array(x)
    raise TypeError('cannot accept object: {}'.format(x))


def _check_same_len(*arrays, axis=0):
    """check if input array list has same length for one dimension

    """
    lens = set([x.shape[axis] for x in arrays if x is not None])
    return len(lens) == 1


def _label_types(y):
    """Determine the type
        - "binary"
        - "multiclass"
        - "multiclass-multioutput"
        - "multilabel"
        - "unknown"
    """
    # never squeeze the first dimension
    y = y.squeeze() if y.shape[0] > 1 else y.resize(1, -1)
    shape = y.shape
    if len(shape) < 1:
        raise ValueError('cannot accept data: {}'.format(y))
    if len(shape) == 1:
        return 'multiclass' if np.unique(y).shape[0] > 2 else 'binary', y
    if len(shape) == 2:
        return 'multiclass-multioutput' if np.unique(y).shape[0] > 2 else 'multilabel', y
    return 'unknown', y


def _check_data(y_true, y_pred):
    """Check if y_true and y_pred is same type of data e.g both binary or multiclass

    """
    y_true, y_pred = _conver_numpy(y_true), _conver_numpy(y_pred)
    if not _check_same_len(y_true, y_pred):
        raise ValueError('cannot accept data with different shape {0}, {1}'.format(y_true, y_pred))
    type_true, y_true = _label_types(y_true)
    type_pred, y_pred = _label_types(y_pred)

    type_set = set(['binary', 'multiclass'])
    if type_true in type_set and type_pred in type_set:
        return type_true if type_true == type_pred else 'multiclass', y_true, y_pred

    type_set = set(['multiclass-multioutput', 'multilabel'])
    if type_true in type_set and type_pred in type_set:
        return type_true if type_true == type_pred else 'multiclass-multioutput', y_true, y_pred

    raise ValueError('cannot accept data mixed of {0} and {1} target'.format(type_true, type_pred))


def _weight_sum(y, normalize=True, sample_weight=None):
    if normalize:
        return np.average(y, weights=sample_weight)
    if sample_weight is None:
        return y.sum()
    else:
        return np.dot(y, sample_weight)


def accuracy_score(y_true, y_pred, normalize=True, sample_weight=None):
    y_type, y_true, y_pred = _check_data(y_true, y_pred)
    if y_type == 'multiclass-multioutput':
        raise ValueError('cannot accept data type {0}'.format(y_type))
    if y_type == 'multilabel':
        equel = (y_true == y_pred).sum(1)
        count = equel == y_true.shape[1]
    else:
        count = y_true == y_pred
    return _weight_sum(count, normalize=normalize, sample_weight=sample_weight)


def recall_score(y_true, y_pred, labels=None, pos_label=1, average='binary'):
    y_type, y_true, y_pred = _check_data(y_true, y_pred)
    if average == 'binary':
        if y_type != 'binary':
            raise ValueError("data type is {} but  use average type {}".format(y_type, average))
        else:
            pos = (y_true == pos_label)
            tp = np.logical_and((y_true == y_pred), pos).sum()
            pos_sum = pos.sum()
            return tp / pos_sum if pos_sum > 0 else 0
    elif average == None:
        y_labels = set(list(np.unique(y_true)))
        if labels is None:
            labels = list(y_labels)
        else:
            for i in labels:
                if (i not in y_labels and y_type != 'multilabel') or (y_type == 'multilabel' and i >= y_true.shape[1]):
                    warnings.warn('label {} is not contained in data'.format(i), UserWarning)

        if y_type in ['binary', 'multiclass']:
            y_pred_right = y_true == y_pred
            pos_list = [y_true == i for i in labels]
            pos_sum_list = [pos_i.sum() for pos_i in pos_list]
            return np.array([np.logical_and(y_pred_right, pos_i).sum() / sum_i if sum_i > 0 else 0 \
                             for pos_i, sum_i in zip(pos_list, pos_sum_list)])
        elif y_type == 'multilabel':
            y_pred_right = y_true == y_pred
            pos = (y_true == pos_label)
            tp = np.logical_and(y_pred_right, pos).sum(0)
            pos_sum = pos.sum(0)
            return np.array([tp[i] / pos_sum[i] if pos_sum[i] > 0 else 0 for i in labels])
        else:
            raise ValueError('not support targets type {}'.format(y_type))
    raise ValueError('not support for average type {}'.format(average))


def precision_score(y_true, y_pred, labels=None, pos_label=1, average='binary'):
    y_type, y_true, y_pred = _check_data(y_true, y_pred)
    if average == 'binary':
        if y_type != 'binary':
            raise ValueError("data type is {} but  use average type {}".format(y_type, average))
        else:
            pos = (y_true == pos_label)
            tp = np.logical_and((y_true == y_pred), pos).sum()
            pos_pred = (y_pred == pos_label).sum()
            return tp / pos_pred if pos_pred > 0 else 0
    elif average == None:
        y_labels = set(list(np.unique(y_true)))
        if labels is None:
            labels = list(y_labels)
        else:
            for i in labels:
                if (i not in y_labels and y_type != 'multilabel') or (y_type == 'multilabel' and i >= y_true.shape[1]):
                    warnings.warn('label {} is not contained in data'.format(i), UserWarning)

        if y_type in ['binary', 'multiclass']:
            y_pred_right = y_true == y_pred
            pos_list = [y_true == i for i in labels]
            pos_sum_list = [(y_pred == i).sum() for i in labels]
            return np.array([np.logical_and(y_pred_right, pos_i).sum() / sum_i if sum_i > 0 else 0 \
                             for pos_i, sum_i in zip(pos_list, pos_sum_list)])
        elif y_type == 'multilabel':
            y_pred_right = y_true == y_pred
            pos = (y_true == pos_label)
            tp = np.logical_and(y_pred_right, pos).sum(0)
            pos_sum = (y_pred == pos_label).sum(0)
            return np.array([tp[i] / pos_sum[i] if pos_sum[i] > 0 else 0 for i in labels])
        else:
            raise ValueError('not support targets type {}'.format(y_type))
    raise ValueError('not support for average type {}'.format(average))


def f1_score(y_true, y_pred, labels=None, pos_label=1, average='binary'):
    precision = precision_score(y_true, y_pred, labels=labels, pos_label=pos_label, average=average)
    recall = recall_score(y_true, y_pred, labels=labels, pos_label=pos_label, average=average)
    if isinstance(precision, np.ndarray):
        res = 2 * precision * recall / (precision + recall)
        res[(precision + recall) <= 0] = 0
        return res
    return 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0


def classification_report(y_true, y_pred, labels=None, target_names=None, digits=2):
    raise NotImplementedError


def accuracy_topk(y_true, y_prob, k=1):
    """Compute accuracy of y_true matching top-k probable
    labels in y_prob.

        :param y_true: ndarray, true label, [n_samples]
        :param y_prob: ndarray, label probabilities, [n_samples, n_classes]
        :param k: int, k in top-k
        :return :accuracy of top-k
    """

    y_pred_topk = np.argsort(y_prob, axis=-1)[:, -1:-k - 1:-1]
    y_true_tile = np.tile(np.expand_dims(y_true, axis=1), (1, k))
    y_match = np.any(y_pred_topk == y_true_tile, axis=-1)
    acc = np.sum(y_match) / y_match.shape[0]

    return acc


def pred_topk(y_prob, k=1):
    """Return top-k predicted labels and corresponding probabilities.


        :param y_prob: ndarray, size [n_samples, n_classes], probabilities on labels
        :param k: int, k of top-k
    :returns
        y_pred_topk: ndarray, size [n_samples, k], predicted top-k labels
        y_prob_topk: ndarray, size [n_samples, k], probabilities for top-k labels
    """

    y_pred_topk = np.argsort(y_prob, axis=-1)[:, -1:-k - 1:-1]
    x_axis_index = np.tile(
        np.arange(len(y_prob))[:, np.newaxis],
        (1, k))
    y_prob_topk = y_prob[x_axis_index, y_pred_topk]
    return y_pred_topk, y_prob_topk
