"""
 Copyright (c) 2019-2020 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""
import inspect
import operator
from collections import OrderedDict
from enum import Enum
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import TypeVar

import functools
import networkx as nx
import torch
from copy import deepcopy

from nncf.common.utils.ordered_enum import OrderedEnum
from nncf.module_operations import UpdateWeight

from nncf.dynamic_graph.graph import NNCFNodeExpression
from torch import nn

from nncf.debug import CombinedDebugInterface
from nncf.debug import debuggable_forward
from nncf.debug import is_debug
from nncf.common.graph.model_transformer import ModelTransformer
from nncf.common.graph.transformations.commands import TargetType
from nncf.common.graph.transformations.commands import TransformationPriority
from nncf.common.graph.module_attributes import BaseModuleAttributes
from nncf.common.graph.module_attributes import ConvolutionModuleAttributes
from nncf.common.graph.module_attributes import GroupNormModuleAttributes
from nncf.dynamic_graph.context import TracingContext
from nncf.dynamic_graph.graph import PTNNCFGraph
from nncf.dynamic_graph.graph import ShapeIgnoringTensorMetaComparator
from nncf.dynamic_graph.graph_builder import GraphBuilder
from nncf.dynamic_graph.graph_builder import ModelInputInfo
from nncf.dynamic_graph.graph_builder import PostGraphBuildActing
from nncf.dynamic_graph.graph_builder import create_dummy_forward_fn
from nncf.dynamic_graph.graph_matching import NodeExpression
from nncf.dynamic_graph.input_wrapping import InputInfoWrapManager
from nncf.dynamic_graph.input_wrapping import MODEL_INPUT_OP_NAME
from nncf.dynamic_graph.operator_metatypes import OPERATOR_METATYPES
from nncf.dynamic_graph.patch_pytorch import ignore_scope
from nncf.dynamic_graph.transform_graph import replace_modules_by_nncf_modules
from nncf.dynamic_graph.transformations.commands import PTInsertionCommand
from nncf.dynamic_graph.transformations.commands import PTTargetPoint
from nncf.hw_config import HWConfig
from nncf.layers import NNCF_GENERAL_CONV_MODULES_DICT
from nncf.layers import NNCF_MODULES
from nncf.layers import NNCF_WRAPPED_USER_MODULES_DICT
from nncf.quantization.layers import QUANTIZATION_MODULES
from nncf.utils import compute_FLOPs_hook
from nncf.utils import get_all_modules_by_type
from nncf.utils import get_state_dict_names_with_modules

MODEL_WRAPPED_BY_NNCF_ATTR_NAME = 'nncf_module'

Module = TypeVar('Module', bound=nn.Module)
ModuleAttributes = TypeVar('ModuleAttributes', bound=BaseModuleAttributes)

class ExtraCompressionModuleType(Enum):
    ACTIVATION_QUANTIZER = 0


class LoadStateListener:
    """
        Resets the initialization flags (`initialized`) for all quantization modules on `load_state_dict` call.
        These flags are used to update not loaded params (from checkpoint or model's state)
        on initialization stage of algorithm.
        Flags reset is required on each call of `load_state_dict`, because internal method (`build_graph`)
        restores model state by calling this method.
    """

    def __init__(self, model, all_quantizations):
        # pylint: disable=protected-access
        self.hook = model._register_load_state_dict_pre_hook(
            functools.partial(self.hook_fn, quantize_modules=all_quantizations.values()))

    def hook_fn(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs,
                quantize_modules):
        for module in quantize_modules:
            module.initialized = False

    def close(self):
        self.hook.remove()


class InsertionPointGraphNodeType(Enum):
    INSERTION_POINT = 0
    OPERATOR = 1


class InsertionPointGraph(nx.DiGraph):
    """
    This graph is built from the NNCFGraph representation of the model control flow graph and adds ephemeral
    "insertion point nodes" into the NNCF model graph representation corresponding to operator pre- and
    post-hooks. Module pre-op and post-op insertion points are currently not reflected here, but they are
    probably not required for quantizing activations, for which the quantizer propagation makes sense.
    This "insertion point graph" representation is useful for quantizer propagation and for referencing
    the compression algorithm hooks to the model operations to which they are applied to.
    """
    NODE_TYPE_NODE_ATTR = "node_type"
    INSERTION_POINT_DATA_NODE_ATTR = "insertion_point_data"
    IS_IN_NNCF_MODULE_NODE_ATTR = "is_in_nncf_module"
    REGULAR_NODE_REF_NODE_ATTR = "regular_node_ref"
    ASSOCIATED_IP_NODE_KEYS_NODE_ATTR = "associated_ip_node_keys"
    OPERATOR_METATYPE_NODE_ATTR = "op_meta"

    PRE_HOOK_ID_PREFIX = "PRE HOOK "  # NB: Do not use colon (':') in node keys! Causes trouble for .dot file export.
    POST_HOOK_ID_PREFIX = "POST HOOK "

    def __init__(self, model_nx_graph: nx.DiGraph):
        super().__init__()
        self._base_nx_graph = deepcopy(model_nx_graph)
        self._input_ips = []  # type: List[PTTargetPoint]

        for node_key, node in self._base_nx_graph.nodes.items():
            attrs = {InsertionPointGraph.REGULAR_NODE_REF_NODE_ATTR: node,
                     InsertionPointGraph.NODE_TYPE_NODE_ATTR: InsertionPointGraphNodeType.OPERATOR,
                     InsertionPointGraph.ASSOCIATED_IP_NODE_KEYS_NODE_ATTR: set(),
                     InsertionPointGraph.OPERATOR_METATYPE_NODE_ATTR: None}
            self.add_node(node_key, **attrs)

        IN_PORT_ID_ATTR_NAME = "in_port_id"
        for edge in self._base_nx_graph.edges:
            in_port_id = self._base_nx_graph.edges[edge][PTNNCFGraph.IN_PORT_NAME_EDGE_ATTR]
            from_node, to_node = edge
            attrs = {IN_PORT_ID_ATTR_NAME: in_port_id}
            self.add_edge(from_node, to_node, **attrs)

        # TODO: Add insertion points for module pre- and post-ops.
        # Should roughly look so: first, determine subsets of nodes belonging to each
        # separate NNCF module (via scope analysis), then for each subset find input/output
        # edges using a corresponding NNCFGraph function; add a pre-op insertion point node as the
        # sink for input edges and connect it to input edge destinations, then add a post-op
        # insertion point as the source of output edges and connect it to output edge origins.

        node_keys_working_set = [deepcopy(node_key) for node_key in self.nodes.keys()]
        for operator_node_key in node_keys_working_set:
            original_node = self.nodes[operator_node_key][InsertionPointGraph.REGULAR_NODE_REF_NODE_ATTR]
            ia_op_exec_context = original_node[PTNNCFGraph.OP_EXEC_CONTEXT_NODE_ATTR].input_agnostic

            # Pre-hook insertion point nodes
            # Will insert a pre-hook IP for each input edge. The input edge must be marked with
            # a port ID attribute.
            operator_node = self.nodes[operator_node_key]
            in_edges = list(self.in_edges(operator_node_key))
            for edge in in_edges:
                port_id = self.edges[edge][IN_PORT_ID_ATTR_NAME]
                from_node_key, to_node_key = edge
                ip_node_key = self.get_pre_hook_node_key(str(operator_node_key), port_id)

                pre_hook_insertion_point = PTTargetPoint(TargetType.OPERATOR_PRE_HOOK,
                                                         ia_op_exec_context=ia_op_exec_context,
                                                         input_port_id=port_id)
                pre_hook_ip_attrs = {
                    InsertionPointGraph.NODE_TYPE_NODE_ATTR: InsertionPointGraphNodeType.INSERTION_POINT,
                    InsertionPointGraph.INSERTION_POINT_DATA_NODE_ATTR: pre_hook_insertion_point,
                }

                self.add_node(ip_node_key, **pre_hook_ip_attrs)

                self.remove_edge(from_node_key, to_node_key)
                self.add_edge(from_node_key, ip_node_key)
                self.add_edge(ip_node_key, operator_node_key)
                operator_node[InsertionPointGraph.ASSOCIATED_IP_NODE_KEYS_NODE_ATTR].add(ip_node_key)

            # Post-hook insertion point nodes
            post_hook_insertion_point = PTTargetPoint(TargetType.OPERATOR_POST_HOOK,
                                                      ia_op_exec_context=ia_op_exec_context)
            post_hook_ip_attrs = {
                InsertionPointGraph.NODE_TYPE_NODE_ATTR: InsertionPointGraphNodeType.INSERTION_POINT,
                InsertionPointGraph.INSERTION_POINT_DATA_NODE_ATTR: post_hook_insertion_point
            }
            ip_node_key = self.get_post_hook_node_key(str(operator_node_key))
            self.add_node(ip_node_key, **post_hook_ip_attrs)
            out_edges = list(self.out_edges(operator_node_key))
            for out_edge in out_edges:
                # Need to preserve original edge attributes in order not to lose
                # input port ID information
                original_edge_attrs = self.edges[out_edge]
                from_node_key, to_node_key = out_edge
                self.remove_edge(from_node_key, to_node_key)
                self.add_edge(ip_node_key, to_node_key, **original_edge_attrs)
                # TODO: introduce separate insertion points for operator outputs if
                # the outputs are semantically different
            self.add_edge(operator_node_key, ip_node_key)
            operator_node = self.nodes[operator_node_key]
            operator_node[InsertionPointGraph.ASSOCIATED_IP_NODE_KEYS_NODE_ATTR].add(ip_node_key)

            if ia_op_exec_context.operator_name == MODEL_INPUT_OP_NAME:
                self._input_ips.append(post_hook_insertion_point)

    def _base_graph_match_has_breaking_edges(self, match):
        # Breaking output edges
        for node_key in match[:-1]:
            succs = list(self._base_nx_graph.succ[node_key].keys())
            for succ_key in succs:
                if succ_key not in match:
                    return True

        # Breaking input edges
        for node_key in match[1:]:
            preds = list(self._base_nx_graph.pred[node_key].keys())
            for pred_key in preds:
                if pred_key not in match:
                    return True
        return False

    def get_ip_graph_with_merged_hw_optimized_operations(self,
                                                         hw_config: Optional[HWConfig] = None,
                                                         additional_patterns: Optional[List[str]] = None) \
            -> 'InsertionPointGraph':
        # pylint:disable=too-many-branches
        merged_ip_graph = deepcopy(self)
        pattern = self._get_mergeable_operator_patterns(hw_config, additional_patterns)
        from nncf.dynamic_graph.graph_matching import search_all
        matches = search_all(self._base_nx_graph, pattern)
        for match in matches:
            if len(match) == 1:
                continue

            input_node_key = match[0]
            output_node_key = match[-1]

            # If a subgraph has output edges in its middle, should skip merging it
            # Example (conv2d + BN + relu pattern):
            #       (conv2d)
            #          |------\
            #         (BN)    |
            #          |      |
            #        (RELU)   |
            #          |      |
            #        (cat)----/
            #          |
            #         ...

            # Same for input edges (linear + add pattern):
            # (linear)      (linear)
            #     |            |
            #     \----(add)---/
            #            |
            #           ...
            has_breaking_output_edges = self._base_graph_match_has_breaking_edges(match)

            if has_breaking_output_edges:
                continue

            in_edges = list(self.in_edges(input_node_key))
            out_edges = list(self.out_edges(output_node_key))

            in_edge_copies_dict = {}
            for in_edge_key in in_edges:
                in_edge_copies_dict[in_edge_key] = deepcopy(self.edges[in_edge_key])
            out_edge_copies_dict = {}
            for out_edge_key in out_edges:
                out_edge_copies_dict[out_edge_key] = deepcopy(self.edges[out_edge_key])

            conserved_edges_list = out_edges + in_edges

            merged_node_attrs = deepcopy(self.nodes[input_node_key])
            merged_node_attrs[InsertionPointGraph.ASSOCIATED_IP_NODE_KEYS_NODE_ATTR] = set()
            merged_node_key = ""
            for node_key in match:
                ip_node_keys = self.nodes[node_key][InsertionPointGraph.ASSOCIATED_IP_NODE_KEYS_NODE_ATTR]
                for ip_node_key in ip_node_keys:
                    should_keep_ip_node = False
                    for edge_key in conserved_edges_list:
                        if ip_node_key in edge_key:
                            should_keep_ip_node = True
                            break
                    if should_keep_ip_node:
                        merged_node_attrs[InsertionPointGraph.ASSOCIATED_IP_NODE_KEYS_NODE_ATTR].add(ip_node_key)
                    else:
                        merged_ip_graph.remove_node(ip_node_key)
                merged_ip_graph.remove_node(node_key)
                merged_node_key += node_key + '\n'

            merged_ip_graph.add_node(merged_node_key, **merged_node_attrs)
            for in_edge_key, in_edge_attrs in in_edge_copies_dict.items():
                merged_ip_graph.add_edge(in_edge_key[0], merged_node_key, **in_edge_attrs)
            for out_edge_key, out_edge_attrs in out_edge_copies_dict.items():
                merged_ip_graph.add_edge(merged_node_key, out_edge_key[1], **out_edge_attrs)

        return merged_ip_graph

    @staticmethod
    def get_pre_hook_node_key(node_key: str, in_port_id: int = 0) -> str:
        return InsertionPointGraph.PRE_HOOK_ID_PREFIX + str(in_port_id) + ' ' + node_key

    @staticmethod
    def get_post_hook_node_key(node_key: str) -> str:
        return InsertionPointGraph.POST_HOOK_ID_PREFIX + node_key

    def _get_mergeable_operator_patterns(self, hw_config: Optional[HWConfig] = None,
                                         additional_patterns: Optional[List[str]] = None) -> NodeExpression:
        """Resulting pattern should have single input; the operation with inputs to
        quantize should be the input operation; outputs should only be produced by one output node."""
        # TODO: Implement "repeating expressions" so that any number of "mergeable" operations
        # immediately following a linear/convolutional/matrix op are merged into one block
        import nncf.dynamic_graph.patterns as p
        full_pattern = p.LINEAR_OPS + p.ANY_BN_ACT_COMBO | p.LINEAR_OPS + p.ELTWISE_UNIFORM_OPS | \
                       p.ARITHMETIC + p.ANY_BN_ACT_COMBO | p.ANY_BN_ACT_COMBO
        if additional_patterns is not None:
            for pattern in additional_patterns:
                if not isinstance(pattern, str):
                    custom_pattern = functools.reduce(operator.add,
                                                      [NNCFNodeExpression(node) for node in pattern])
                else:
                    custom_pattern = NNCFNodeExpression(pattern)
                full_pattern = full_pattern | custom_pattern
        return full_pattern

    def get_op_nodes_in_scope(self, scope: 'Scope') -> List:
        matching_ip_graph_op_nodes_list = []
        for node in self.nodes().values():
            if node[InsertionPointGraph.NODE_TYPE_NODE_ATTR] == InsertionPointGraphNodeType.OPERATOR:
                nncf_graph_node_ref = node[InsertionPointGraph.REGULAR_NODE_REF_NODE_ATTR]
                op_exec_context = nncf_graph_node_ref[PTNNCFGraph.OP_EXEC_CONTEXT_NODE_ATTR]
                op_scope = op_exec_context.input_agnostic.scope_in_model
                if op_scope in scope:
                    matching_ip_graph_op_nodes_list.append(node)
        return matching_ip_graph_op_nodes_list

    def get_input_insertion_points(self) -> List[PTTargetPoint]:
        return self._input_ips



class PTInsertionType(OrderedEnum):
    NNCF_MODULE_PRE_OP = 0
    NNCF_MODULE_POST_OP = 1
    OPERATOR_PRE_HOOK = 2
    OPERATOR_POST_HOOK = 3


class PTInsertionPoint:
    TARGET_TYPE_VS_PT_INSERTION_TYPE_DICT = {
        TargetType.PRE_LAYER_OPERATION: PTInsertionType.NNCF_MODULE_PRE_OP,
        TargetType.POST_LAYER_OPERATION: PTInsertionType.NNCF_MODULE_POST_OP,
        TargetType.OPERATION_WITH_WEIGHTS: PTInsertionType.NNCF_MODULE_PRE_OP,
        TargetType.OPERATOR_PRE_HOOK: PTInsertionType.OPERATOR_PRE_HOOK,
        TargetType.OPERATOR_POST_HOOK: PTInsertionType.OPERATOR_POST_HOOK
    }

    def _get_pt_insertion_type(self, target_type: TargetType) -> PTInsertionType:
        if target_type not in PTInsertionPoint.TARGET_TYPE_VS_PT_INSERTION_TYPE_DICT:
            raise RuntimeError("Unsupported target type for PyTorch: {}".format(target_type))
        return PTInsertionPoint.TARGET_TYPE_VS_PT_INSERTION_TYPE_DICT[target_type]

    def __init__(self, target_point: PTTargetPoint):
        self.insertion_type = self._get_pt_insertion_type(target_point.target_type)
        self.ia_op_exec_context = target_point.ia_op_exec_context
        self.module_scope = target_point.module_scope
        self.input_port_id = target_point.input_port_id

    def __eq__(self, other: 'PTInsertionPoint'):
        return self.insertion_type == other.insertion_type and \
               self.ia_op_exec_context == other.ia_op_exec_context and \
               self.module_scope == other.module_scope and \
               self.input_port_id == other.input_port_id

    def __str__(self):
        return ' '.join([str(v) for v in self.__dict__.values()])

    def __hash__(self):
        return hash(str(self))


# pylint: disable=too-many-public-methods


@ignore_scope
class NNCFNetwork(nn.Module, PostGraphBuildActing):
    def __init__(self, module, input_infos: List[ModelInputInfo],
                 dummy_forward_fn=None, wrap_inputs_fn=None, scopes_without_shape_matching=None,
                 ignored_scopes=None, target_scopes=None, reset: bool = False):
        super().__init__()
        self._set_nncf_wrapped_model(module)
        self._forward_signature = inspect.signature(module.forward)
        self.input_infos = input_infos

        self.ignored_scopes = ignored_scopes
        self.target_scopes = target_scopes
        self._user_dummy_forward_fn = dummy_forward_fn

        device = next(module.parameters()).device

        if wrap_inputs_fn is not None:
            self._wrap_inputs_fn = wrap_inputs_fn
        else:
            self.__input_infos_based_input_wrapper = InputInfoWrapManager(self.input_infos,
                                                                          self._forward_signature,
                                                                          module_ref_for_device=self)
            self._wrap_inputs_fn = self.__input_infos_based_input_wrapper.wrap_inputs

        self._nncf_module_scopes = []  # type: List[Scope]
        self.scopes_without_shape_matching = scopes_without_shape_matching
        self.debug_interface = CombinedDebugInterface() if is_debug() else None
        self._extra_module_types = []  # type: List[ExtraCompressionModuleType]
        # pylint:disable=line-too-long
        self._insertions_into_original_graph = {}  # type: Dict[PTTargetPoint, List[Tuple[Callable, TransformationPriority]]]

        _orig_graph_build_forward_fn = self._get_dummy_forward_fn_for_graph_building(with_input_tracing=True)
        self._graph_builder = GraphBuilder(_orig_graph_build_forward_fn)

        nncf_wrapped_model = self.get_nncf_wrapped_model()
        eval_only_ops_exec_ctx = self.collect_eval_only_ops_exec_context(nncf_wrapped_model, self._graph_builder)

        # all modules called in eval mode should be replaced prior to graph building
        self._replace_modules_by_nncf_modules(device, eval_only_ops_exec_ctx, reset)

        _orig_context = TracingContext()

        _orig_context.add_node_comparators([MODEL_INPUT_OP_NAME], ShapeIgnoringTensorMetaComparator())
        if self.scopes_without_shape_matching:
            _orig_context.add_node_comparators(scopes_without_shape_matching,
                                               ShapeIgnoringTensorMetaComparator())

        self._original_graph = self._graph_builder.build_graph(nncf_wrapped_model, _orig_context,
                                                               as_eval=True)
        self._mark_original_graph_nodes_with_module_attributes()

        self._compressed_context = TracingContext()

        self._dummy_forward_fn = self._get_dummy_forward_fn_for_graph_building(with_input_tracing=False)

        self._compressed_context.add_node_comparators([MODEL_INPUT_OP_NAME], ShapeIgnoringTensorMetaComparator())
        if self.scopes_without_shape_matching:
            self._compressed_context.add_node_comparators(scopes_without_shape_matching,
                                                          ShapeIgnoringTensorMetaComparator())
        self._load_listener = None

    @debuggable_forward
    def forward(self, *args, **kwargs):
        with self._compressed_context as ctx:  # type: TracingContext
            ctx.base_module_thread_local_replica = self
            args, kwargs = self._wrap_inputs_fn(args, kwargs)
            retval = self.get_nncf_wrapped_model()(*args, **kwargs)
        return retval


    # Cannnot use property syntax here, otherwise the wrapped module will end up
    # being twice in the same checkpoint with different prefixes
    def get_nncf_wrapped_model(self):
        return getattr(self, MODEL_WRAPPED_BY_NNCF_ATTR_NAME)

    def _set_nncf_wrapped_model(self, value):
        setattr(self, MODEL_WRAPPED_BY_NNCF_ATTR_NAME, value)

    def get_clean_shallow_copy(self) -> 'NNCFNetwork':
        # WARNING: Will reset pre- and post-ops of the underlying model. Use save_nncf_module_additions
        # and load_nncf_module_additions to preserve these, or temporary_clean_view().
        return NNCFNetwork(self.get_nncf_wrapped_model(), self.input_infos,
                           self._user_dummy_forward_fn, self._wrap_inputs_fn,
                           self.scopes_without_shape_matching, self.ignored_scopes, self.target_scopes,
                           reset=True)

    def get_modules_in_nncf_modules_by_type(self, types) -> Dict['Scope', nn.Module]:
        nncf_modules = self.get_nncf_modules()
        retval = {}
        for nncf_module_scope, nncf_module in nncf_modules.items():
            nncf_module_scope.pop()
            for relative_scope, target_module in get_all_modules_by_type(nncf_module, types).items():
                retval[nncf_module_scope + relative_scope] = target_module
        return retval

    def insert_at_point(self, point: PTInsertionPoint, fn_list: List[Callable]):
        if point.insertion_type == PTInsertionType.OPERATOR_PRE_HOOK:
            self._compressed_context.register_pre_hooks(fn_list, point.ia_op_exec_context, point.input_port_id)
        elif point.insertion_type == PTInsertionType.OPERATOR_POST_HOOK:
            self._compressed_context.register_post_hooks(fn_list, point.ia_op_exec_context)
        elif point.insertion_type in [PTInsertionType.NNCF_MODULE_PRE_OP,
                                      PTInsertionType.NNCF_MODULE_POST_OP]:
            norm_target_scope = self._normalize_variable_recurrent_scope(point.module_scope)
            norm_nncf_scopes = [self._normalize_variable_recurrent_scope(x) for x in self._nncf_module_scopes]
            assert norm_target_scope in norm_nncf_scopes  # Required for proper Recurrent/VariableRecurrent addressing
            nncf_module = self.get_module_by_scope(point.module_scope)
            if point.insertion_type == PTInsertionType.NNCF_MODULE_PRE_OP:
                for fn in fn_list:
                    nncf_module.register_pre_forward_operation(fn)
            elif point.insertion_type == PTInsertionType.NNCF_MODULE_POST_OP:
                for fn in fn_list:
                    nncf_module.register_post_forward_operation(fn)
        else:
            raise RuntimeError("Unsupported insertion type: {}".format(point.insertion_type))

    def __getattr__(self, name):
        wrapped_module = super().__getattr__(MODEL_WRAPPED_BY_NNCF_ATTR_NAME)
        if hasattr(wrapped_module, name):
            return getattr(wrapped_module, name)
        return super().__getattr__(name)

    def get_graph(self) -> PTNNCFGraph:
        if self._compressed_context.graph.get_nodes_count() == 0:
            self.rebuild_graph()
        return self._compressed_context.graph

    def get_original_graph(self) -> PTNNCFGraph:
        return self._original_graph

    def get_tracing_context(self) -> TracingContext:
        return self._compressed_context

    def _get_dummy_forward_fn_for_graph_building(self, with_input_tracing):
        if self._user_dummy_forward_fn is None:
            return create_dummy_forward_fn(self.input_infos,
                                           with_input_tracing=with_input_tracing,
                                           wrap_inputs_fn=self._wrap_inputs_fn)
        return self._user_dummy_forward_fn

    def _replace_modules_by_nncf_modules(self, device, eval_only_ops_exec_ctx: List[str] = None,
                                         reset: bool = False):
        module, self._nncf_module_scopes = replace_modules_by_nncf_modules(
            self.get_nncf_wrapped_model(), ignored_scopes=self.ignored_scopes,
            target_scopes=self.target_scopes, eval_ops_exec_ctx_str=eval_only_ops_exec_ctx,
            reset=reset)
        self._set_nncf_wrapped_model(module.to(device))

    def get_nncf_module_scopes(self) -> List['Scope']:
        return self._nncf_module_scopes

    def get_nncf_modules(self) -> Dict['Scope', torch.nn.Module]:
        nncf_module_names_list = NNCF_MODULES + [x.__name__ for x in NNCF_WRAPPED_USER_MODULES_DICT.values()]
        return get_all_modules_by_type(self.get_nncf_wrapped_model(), nncf_module_names_list)

    def get_nncf_modules_by_module_names(self, nncf_module_names_list: List[str]) -> Dict["Scope", torch.nn.Module]:
        return get_all_modules_by_type(self.get_nncf_wrapped_model(), nncf_module_names_list)

    def rebuild_graph(self, *input_args):
        self._compressed_context.reset_graph()
        dummy_forward_fn = self._get_dummy_forward_fn_for_graph_building(with_input_tracing=False)
        builder = GraphBuilder(dummy_forward_fn)
        _ = builder.build_graph(self, self._compressed_context)

    def post_build_graph_actions(self):
        # Reset initialization flags (`initialized`) for all quantization modules
        # after dummy `load_state_dict` call.
        quantization_types = [class_type.__name__ for class_type in QUANTIZATION_MODULES.registry_dict.values()]
        all_quantizations = get_state_dict_names_with_modules(self, quantization_types)
        for module in all_quantizations.values():
            module.initialized = False

    def is_scope_in_nncf_module_scope(self, scope: 'Scope'):
        # TODO: optimize
        norm_nncf_scopes = [self._normalize_variable_recurrent_scope(x) for x in self._nncf_module_scopes]
        norm_op_scope = self._normalize_variable_recurrent_scope(scope)
        for nncf_scope in norm_nncf_scopes:
            if norm_op_scope in nncf_scope:
                return True
        return False

    def register_compression_module_type(self, compression_module_type: ExtraCompressionModuleType):
        attr_name = self._compression_module_type_to_attr_name(compression_module_type)
        if compression_module_type in self._extra_module_types:
            raise RuntimeError("Module type {} is already registered".format(compression_module_type))
        self.__setattr__(attr_name, nn.ModuleDict())
        self._extra_module_types.append(compression_module_type)

    def add_compression_module(self, module_key: str, module: nn.Module,
                               compression_module_type: ExtraCompressionModuleType):
        attr_name = self._compression_module_type_to_attr_name(compression_module_type)
        if compression_module_type not in self._extra_module_types:
            raise RuntimeError("Module type {} was not registered".format(compression_module_type))
        storage = self.__getattr__(attr_name)
        if module_key in storage:
            raise RuntimeError("Module {} is already registered under {}".format(module_key, attr_name))
        storage[module_key] = module

    def get_compression_modules_by_type(self, compression_module_type: ExtraCompressionModuleType) -> nn.ModuleDict:
        attr_name = self._compression_module_type_to_attr_name(compression_module_type)
        if compression_module_type not in self._extra_module_types:
            raise RuntimeError("Module type {} was not registered".format(compression_module_type))
        return self.__getattr__(attr_name)

    @staticmethod
    def _compression_module_type_to_attr_name(compression_module_type: ExtraCompressionModuleType):
        """Required for backward compatibility with checkpoints that store function and activation
        quantizers directly under corresponding attributes of NNCFNetwork."""
        if compression_module_type == ExtraCompressionModuleType.ACTIVATION_QUANTIZER:
            return "activation_quantizers"
        raise RuntimeError("Unknown extra module type")

    def sort_compression_modules(self, compression_module_type: ExtraCompressionModuleType):
        attr_name = self._compression_module_type_to_attr_name(compression_module_type)
        if compression_module_type not in self._extra_module_types:
            raise RuntimeError("Module type {} was not registered".format(compression_module_type))
        module_dict = self.__getattr__(attr_name)
        # pylint: disable=protected-access
        module_dict._modules = OrderedDict(sorted(module_dict._modules.items()))
        self.__setattr__(attr_name, module_dict)

    @staticmethod
    def _normalize_variable_recurrent_scope(scope: 'Scope'):
        """
        Two scopes pointing to an NNCF module that only differ in a Recurrent/VariableRecurrent/VariableRecurrentReverse
        scope element actually point to one and the same module.
        """
        ret_scope = scope.copy()
        for scope_element in ret_scope:
            if scope_element.calling_module_class_name in ["Recurrent", "VariableRecurrent",
                                                           "VariableRecurrentReverse"]:
                scope_element.calling_module_class_name = "NormalizedName_Recurrent"
        return ret_scope

    def do_dummy_forward(self, force_eval=False):
        """Attention: If run with force_eval=False, this may spoil the batchnorm statistics,
        and an eval run of the model will perform much worse than the train run. """
        if force_eval:
            train_mode = self.training
            self.eval()
        with torch.no_grad():
            self._dummy_forward_fn(self)
        if force_eval:
            if train_mode:
                self.train()

    def get_input_shape_for_insertion_point(self, insertion_point: PTTargetPoint) -> Tuple[int]:
        ia_op_exec_context = insertion_point.ia_op_exec_context
        if insertion_point.input_port_id is not None:
            quantizer_input_shape = self._original_graph.get_input_shapes_for_ia_op_exec_context(
                ia_op_exec_context)[insertion_point.input_port_id]
        else:
            # Tailored for post-hook quantization and first output quantization only
            quantizer_input_shape = self._original_graph.get_output_shapes_for_ia_op_exec_context(
                ia_op_exec_context)[0]
        return quantizer_input_shape

    def get_insertion_point_graph(self) -> InsertionPointGraph:
        ip_graph = InsertionPointGraph(self._original_graph.get_nx_graph_copy())

        # Mark IP graph operator nodes with associated op metatypes
        # Determining operator metatypes is more suited to occur at wrap_operator
        # stage, because it might be influenced by specific non-tensor function paramters,
        # but we have to inspect the containing module parameters as well, so the
        # TracingContext in wrap_operator would have to retain a reference to
        # the model that uses it. Since currently we do not need to inspect the
        # function arguments to determine the metatype, we can do this here, but
        # once we need to inspect the arguments, the code will have to be moved to
        # wrap_operator.

        for node_key in ip_graph.nodes:
            ip_graph_node = ip_graph.nodes[node_key]
            ip_graph_node_type = ip_graph_node[InsertionPointGraph.NODE_TYPE_NODE_ATTR]
            if ip_graph_node_type == InsertionPointGraphNodeType.OPERATOR:
                nncf_graph_node_ref = ip_graph_node[InsertionPointGraph.REGULAR_NODE_REF_NODE_ATTR]
                op_arch = self.get_op_arch_by_graph_node(nncf_graph_node_ref)
                ip_graph_node[InsertionPointGraph.OPERATOR_METATYPE_NODE_ATTR] = op_arch
        return ip_graph

    def get_op_arch_by_graph_node(self, nncf_graph_node_ref: dict) -> 'OperatorMetatype':
        op_exec_context = nncf_graph_node_ref[PTNNCFGraph.OP_EXEC_CONTEXT_NODE_ATTR]
        op_name = op_exec_context.operator_name
        scope = op_exec_context.scope_in_model
        op_arch = OPERATOR_METATYPES.get_operator_metatype_by_op_name(op_name)
        module = self.get_module_by_scope(scope)
        if module is not None:
            subtype = op_arch.determine_subtype(containing_module=module)
            if subtype is not None:
                op_arch = subtype
        return op_arch

    def get_module_by_scope(self, scope: 'Scope') -> torch.nn.Module:
        curr_module = self.get_nncf_wrapped_model()
        for scope_element in scope[1:]:  # omit first scope element which corresponds to base module
            if scope_element.calling_field_name is None:
                # The module used is being created in-place every time and never stored in the model,
                # happens for nn.Softmax in BERT implementations.
                return None
            # pylint: disable=protected-access
            next_module = curr_module._modules.get(scope_element.calling_field_name)
            if next_module is None:
                raise RuntimeError("Could not find a {} module member in {} module of scope {} during node search"
                                   .format(scope_element.calling_field_name,
                                           scope_element.calling_module_class_name,
                                           str(scope)))
            curr_module = next_module
        return curr_module

    def get_parameters_count_in_model(self):
        """
        Return total amount of model parameters.
        """
        count = 0
        for param in self.parameters():
            count = count + param.numel()
        return count

    def get_flops_per_module(self) -> Dict['Scope', int]:
        """
        Calculates FLOPS count for modules.
        """
        model = self
        flops_count_dict = {}

        def get_hook(name):
            ctx = self._compressed_context
            return functools.partial(compute_FLOPs_hook, dict_to_save=flops_count_dict, ctx=ctx)

        hook_list = [m.register_forward_hook(get_hook(n)) for n, m in model.named_modules()]

        model.do_dummy_forward(force_eval=True)

        for h in hook_list:
            h.remove()
        return flops_count_dict

    def get_MACs_in_model(self):
        """
            Calculates MAC units count for model.
        """
        flops_count_dict = self.get_flops_per_module()
        total_MACs_count = sum(v // 2 for v in flops_count_dict.values())
        return total_MACs_count

    def get_input_infos(self) -> List[ModelInputInfo]:
        return deepcopy(self.input_infos)

    def save_nncf_module_additions(self) -> Dict['Scope', Tuple[torch.nn.ModuleDict, torch.nn.ModuleDict]]:
        retval = {}
        for module_scope, nncf_module in self.get_nncf_modules().items():
            retval[module_scope] = (deepcopy(nncf_module.pre_ops), deepcopy(nncf_module.post_ops))
        return retval

    def load_nncf_module_additions(self,
                                   scope_vs_pre_post_ops_dict: Dict['Scope', Tuple[torch.nn.ModuleDict,
                                                                                   torch.nn.ModuleDict]]):
        for module_scope, nncf_module in self.get_nncf_modules().items():
            nncf_module.pre_ops = scope_vs_pre_post_ops_dict[module_scope][0]
            nncf_module.post_ops = scope_vs_pre_post_ops_dict[module_scope][1]

    def temporary_clean_view(self):
        class Mgr:
            def __init__(self, model: NNCFNetwork):
                self.model = model
                self.storage_dict = {}

            def __enter__(self):
                self.storage_dict = self.model.save_nncf_module_additions()
                clean_model = self.model.get_clean_shallow_copy()
                return clean_model

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.model.load_nncf_module_additions(self.storage_dict)

        return Mgr(self)

    @staticmethod
    def collect_eval_only_ops_exec_context(model: nn.Module, graph_builder) -> List[str]:
        """
        Returns scopes of the modules which are executed in evaluation mode only.
        """
        result = []
        eval_graph = graph_builder.build_graph(model, as_eval=True)
        for node_key in eval_graph.get_all_node_keys():
            node = eval_graph.get_nx_node_by_key(node_key)
            op_exec_context = node[PTNNCFGraph.OP_EXEC_CONTEXT_NODE_ATTR]
            if op_exec_context:
                result.append(str(op_exec_context.input_agnostic))
        return result

    def _mark_original_graph_nodes_with_module_attributes(self):
        node_types = [v.op_func_name for v in NNCF_GENERAL_CONV_MODULES_DICT] + ["group_norm"]
        for node in self._original_graph.get_nodes_by_types(node_types):
            scope = node.op_exec_context.scope_in_model
            input_agnostic = node.op_exec_context.input_agnostic
            module = self.get_module_by_scope(scope)
            nx_node = self._original_graph.find_node_in_nx_graph_by_input_agnostic(input_agnostic)
            nx_node[PTNNCFGraph.MODULE_ATTRIBUTES] = _get_module_attributes(module, node.op_exec_context.operator_name)


def _get_module_attributes(module: Module, operator_name: str) -> ModuleAttributes:
    if operator_name == "group_norm":
        return GroupNormModuleAttributes(
            module.weight.requires_grad,
            module.num_channels,
            module.num_groups
        )
    return ConvolutionModuleAttributes(
        module.weight.requires_grad,
        module.in_channels,
        module.out_channels,
        module.stride,
        module.groups
    )


class PTModelTransformer(ModelTransformer):
    def transform(self) -> NNCFNetwork:
        fns_grouped_by_points = {}  # type: Dict[PTInsertionPoint, List[Tuple[Callable, TransformationPriority]]]
        for transformation_command in self._transformations:  # type: PTInsertionCommand
            target_point = transformation_command.target_point
            pt_ip = PTInsertionPoint(target_point)
            fn = transformation_command.fn
            if target_point.type is TargetType.OPERATION_WITH_WEIGHTS:
                fn = UpdateWeight(fn)
            tup = (fn, transformation_command.priority)
            if pt_ip not in fns_grouped_by_points:
                fns_grouped_by_points[pt_ip] = [tup]
            else:
                fns_grouped_by_points[pt_ip].append(tup)

        for pt_ip, fn_list_with_priority in fns_grouped_by_points.items():
            fn_list_with_priority = sorted(fn_list_with_priority, key=lambda x: x[1])
            self._model.insert_at_point(pt_ip, [x[0] for x in fn_list_with_priority])
        return self._model
