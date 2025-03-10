# SPDX-FileCopyrightText: Copyright (c) 2022-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import sys
import unittest

import _utils

# isort: off
import torch
import tensorrt as trt
# isort: on
from parameterized import parameterized
from polygraphy.backend.trt import CreateConfig, EngineFromNetwork, TrtRunner

import tensorrt_llm
from tensorrt_llm import Tensor
from tensorrt_llm.quantization.functional import \
    weight_only_groupwise_quant_matmul

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.util import getSMVersion


class TestWeightOnlyGroupWiseQuantMatmul(unittest.TestCase):

    def setUp(self):
        tensorrt_llm.logger.set_level('error')

    def _run_matmul_plugin(self,
                           th_activation,
                           th_pre_quant_scale,
                           th_weight,
                           th_scale,
                           th_zero,
                           th_bias,
                           th_alpha,
                           dtype,
                           quant_algo,
                           group_size=128):
        # Create builder
        builder = tensorrt_llm.Builder()
        net = builder.create_network()
        net.plugin_config.set_weight_only_groupwise_quant_matmul_plugin(dtype)
        with tensorrt_llm.net_guard(net):
            network = tensorrt_llm.default_trtnet()
            # Init TensorRT-LLM tensor for activation
            activation = Tensor(
                name='activation',
                shape=th_activation.shape,
                dtype=tensorrt_llm._utils.str_dtype_to_trt(dtype))
            # Init TensorRT-LLM tensor for pre_quant_scale
            pre_quant_scale = Tensor(
                name='pre_quant_scale',
                shape=th_pre_quant_scale.shape,
                dtype=tensorrt_llm._utils.str_dtype_to_trt(dtype))
            # Init TensorRT-LLM tensor for weight
            weight = Tensor(
                name='weight',
                shape=th_weight.shape,
                dtype=tensorrt_llm._utils.str_dtype_to_trt("float16"))
            # Init TensorRT-LLM tensor for scale
            scale = Tensor(name='scale',
                           shape=th_scale.shape,
                           dtype=tensorrt_llm._utils.str_dtype_to_trt(dtype))
            # Init TensorRT-LLM tensor for zero
            zero = Tensor(name='zero',
                          shape=th_zero.shape,
                          dtype=tensorrt_llm._utils.str_dtype_to_trt(dtype))
            # Init TensorRT-LLM tensor for bias
            bias = Tensor(name='bias',
                          shape=th_bias.shape,
                          dtype=tensorrt_llm._utils.str_dtype_to_trt(dtype))
            # Init TensorRT-LLM tensor for alpha
            alpha = Tensor(
                name='alpha',
                shape=th_alpha.shape,
                dtype=tensorrt_llm._utils.str_dtype_to_trt("float32"))

            # Get output tensor for WBQ Matmul
            output = weight_only_groupwise_quant_matmul(activation,
                                                        pre_quant_scale, weight,
                                                        scale, zero, bias,
                                                        alpha, quant_algo,
                                                        group_size).trt_tensor
            output.name = 'output'
            network.mark_output(output)
            output.dtype = tensorrt_llm._utils.str_dtype_to_trt(dtype)

        # Build engine consisting of only WBQ Matmul
        build_engine = EngineFromNetwork(
            (builder.trt_builder, net.trt_network),
            config=CreateConfig(
                fp16=(dtype == "float16"),
                memory_pool_limits={trt.MemoryPoolType.WORKSPACE: 33554432}))

        # Infer engine
        with TrtRunner(build_engine) as runner:
            outputs = runner.infer(
                feed_dict={
                    'activation': th_activation.numpy(),
                    'pre_quant_scale': th_pre_quant_scale.numpy(),
                    'weight': th_weight.numpy(),
                    'scale': th_scale.numpy(),
                    'zero': th_zero.numpy(),
                    'bias': th_bias.numpy(),
                    'alpha': th_alpha.numpy()
                })

        return torch.tensor(outputs['output'])

    def _woq_groupwise_matmul(self,
                              m,
                              n,
                              k,
                              dtype,
                              has_pre_quant,
                              has_zero,
                              has_bias,
                              group_size=128,
                              uint4_input=True,
                              use_w4a8_awq=False):
        # Init operands for multiplication in int32
        torch.manual_seed(0)
        activation = _utils.woq_gen_weights(m, k, dtype)
        pre_quant_scale = _utils.woq_gen_weights(1, k, dtype)
        qweight_unprocessed = torch.randint(-2**31, 2**31, (k // 8, n)).int()
        scale = _utils.woq_gen_weights(
            (k + group_size) // group_size, n, dtype) * 2
        zero = _utils.woq_gen_weights(
            (k + group_size) //
            group_size, n, dtype) * 2 if has_zero else torch.Tensor().half()
        bias = _utils.woq_gen_weights(
            1, n, dtype) if has_bias else torch.Tensor().half()
        fp8_alpha = torch.randn(
            1, dtype=torch.float32) + 0.5 if use_w4a8_awq else torch.Tensor(
            ).float()
        # Flags for indicating whether the corresponding inputs are applied in quant_algo
        BIAS = 1
        ZERO = 2
        PRE_QUANT_SCALE = 4
        W4A8_AWQ = 8

        quant_algo = use_w4a8_awq * W4A8_AWQ + has_pre_quant * PRE_QUANT_SCALE + has_zero * ZERO + has_bias * BIAS

        packer = torch.ops.trtllm.pack_int8_tensor_to_packed_int4
        preprocessor = torch.ops.trtllm.preprocess_weights_for_mixed_gemm
        qweight_int8 = _utils.woq_groupwise_extract_int4(
            qweight_unprocessed, uint4_input).char()
        qweight_int4x2_interleaved = preprocessor(
            packer(qweight_int8 - uint4_input * 8),
            torch.quint4x2).view(torch.float16)

        input_rows = qweight_int8.shape[0]
        scale_ref = scale.repeat_interleave(group_size, dim=0)[input_rows, :]
        ref_th_weight = qweight_int8.half(
        ) * scale_ref - uint4_input * 8 * scale_ref

        if has_zero:
            zero_ref = zero.repeat_interleave(group_size, dim=0)[input_rows, :]
            ref_th_weight += zero_ref

        activation = torch.zeros_like(activation)
        bias = torch.ones_like(bias)
        output = self._run_matmul_plugin(activation, pre_quant_scale,
                                         qweight_int4x2_interleaved, scale,
                                         zero, bias, fp8_alpha, dtype,
                                         quant_algo, group_size).cpu()

        if use_w4a8_awq:
            activation *= fp8_alpha

        if has_pre_quant:
            pre_quant_scale = pre_quant_scale.repeat(m, 1)
            activation = torch.mul(activation, pre_quant_scale)

        ref = _utils.woq_groupwise_gt_matmul(activation, ref_th_weight, bias)

        _utils.woq_assert_colwise_near_eq(ref, output, 2)

    @parameterized.expand([(1, 1024, 64, 'float16', False, True, True, 64),
                           (16, 1024, 256, 'float16', False, True, False, 64),
                           (32, 2048, 384, 'float16', False, False, True, 64),
                           (64, 2048, 1024, 'float16', False, False, False, 64),
                           (1, 1024, 128, 'float16', False, True, True, 128),
                           (16, 1024, 256, 'float16', False, True, False, 128),
                           (32, 2048, 384, 'float16', False, False, True, 128),
                           (64, 2048, 1024, 'float16', False, False, False, 128)
                           ])
    @unittest.skipIf(getSMVersion() < 80, "Unsupported test on pre-Ampere.")
    def test_matmul_int4_input(self,
                               m,
                               n,
                               k,
                               dtype,
                               has_pre_quant,
                               has_zero,
                               has_bias,
                               group_size=128):
        self._woq_groupwise_matmul(m,
                                   n,
                                   k,
                                   dtype,
                                   has_pre_quant,
                                   has_zero,
                                   has_bias,
                                   group_size,
                                   uint4_input=(getSMVersion() < 90))

    @parameterized.expand([(1, 1024, 64, 'float16', True, True, True, 64),
                           (16, 1024, 256, 'float16', True, True, False, 64),
                           (32, 2048, 384, 'float16', True, False, True, 64),
                           (64, 2048, 1024, 'float16', True, False, False, 64),
                           (1, 1024, 128, 'float16', True, True, True, 128),
                           (16, 1024, 256, 'float16', True, True, False, 128),
                           (32, 2048, 384, 'float16', True, False, True, 128),
                           (64, 2048, 1024, 'float16', True, False, False, 128)]
                          )
    @unittest.skipIf(getSMVersion() < 80, "Unsupported test on pre-Ampere.")
    def test_prequant_matmul_int4_input(self,
                                        m,
                                        n,
                                        k,
                                        dtype,
                                        has_pre_quant,
                                        has_zero,
                                        has_bias,
                                        group_size=128):
        self._woq_groupwise_matmul(m,
                                   n,
                                   k,
                                   dtype,
                                   has_pre_quant,
                                   has_zero,
                                   has_bias,
                                   group_size,
                                   uint4_input=(getSMVersion() < 90))

    @parameterized.expand([
        (1, 1024, 128, 'float16', True, True, True, 64, False),
        (1, 1024, 128, 'float16', True, True, True, 128, False),
        (16, 1024, 256, 'float16', True, True, False, 128, True),
        (32, 2048, 384, 'float16', True, False, True, 128, False),
        (64, 2048, 1024, 'float16', True, False, False, 128, True)
    ])
    @unittest.skipIf(getSMVersion() != 90,
                     "Hopper dedicated test, not supported on pre-Hopper.")
    def test_prequant_matmul_fp8_int4_input_hopper(self, m, n, k, dtype,
                                                   has_pre_quant, has_zero,
                                                   has_bias, group_size,
                                                   use_w4a8_awq):
        self._woq_groupwise_matmul(m,
                                   n,
                                   k,
                                   dtype,
                                   has_pre_quant,
                                   has_zero,
                                   has_bias,
                                   group_size,
                                   uint4_input=False,
                                   use_w4a8_awq=use_w4a8_awq)

    # On hopper, any multiple of 64 works as a group size for FP16, with the CUTLASS kernel
    # We keep some unit tests to ensure that this support is maintained, even if the CUDA kernels
    # do not support it at the moment.
    @parameterized.expand([(32, 1024, 128, 'float16', True, True, True, 64),
                           (32, 1024, 128, 'float16', True, True, True, 128),
                           (32, 1024, 256, 'float16', True, True, False, 192),
                           (32, 2048, 384, 'float16', True, False, True, 256),
                           (64, 2048, 1024, 'float16', True, False, False, 320)]
                          )
    @unittest.skipIf(getSMVersion() != 90,
                     "Hopper dedicated test, not supported on pre-Hopper.")
    def test_hopper_fp16_int4_flexible_groups(self, m, n, k, dtype,
                                              has_pre_quant, has_zero, has_bias,
                                              group_size):
        self._woq_groupwise_matmul(m,
                                   n,
                                   k,
                                   dtype,
                                   has_pre_quant,
                                   has_zero,
                                   has_bias,
                                   group_size,
                                   uint4_input=False)

    # On hopper, any multiple of 128 works as a group size for FP8, with the CUTLASS kernel
    # We keep some unit tests to ensure that this support is maintained, even if the CUDA kernels
    # do not support it at the moment.
    @parameterized.expand([(32, 1024, 128, 'float16', True, True, True, 128),
                           (32, 1024, 128, 'float16', True, True, True, 256),
                           (32, 1024, 256, 'float16', True, True, False, 384),
                           (32, 2048, 1024, 'float16', True, False, True, 512),
                           (64, 2048, 2048, 'float16', True, False, False, 640)]
                          )
    @unittest.skipIf(getSMVersion() != 90,
                     "Hopper dedicated test, not supported on pre-Hopper.")
    def test_hopper_fp8_int4_flexible_groups(self, m, n, k, dtype,
                                             has_pre_quant, has_zero, has_bias,
                                             group_size):
        self._woq_groupwise_matmul(m,
                                   n,
                                   k,
                                   dtype,
                                   has_pre_quant,
                                   has_zero,
                                   has_bias,
                                   group_size,
                                   uint4_input=False,
                                   use_w4a8_awq=True)


if __name__ == '__main__':
    unittest.main()
