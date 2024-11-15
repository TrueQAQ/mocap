import numpy as np

def semantic_grid_trans(src_graph_pose:np.ndarray):
    """
    1 x 17 x 2 -> 1 x 2 x 5 x 5
    """
    assert len(src_graph_pose.shape) == 3 # B*J*C
    batch_size, _, C = src_graph_pose.shape
    grid_pose = np.zeros([batch_size, 5, 5, C])
    grid_pose[:, 0] = src_graph_pose[:, [7, 7, 7, 7, 7]]
    grid_pose[:, 1] = src_graph_pose[:, [0, 8, 8, 8, 0]]
    grid_pose[:, 2] = src_graph_pose[:, [1, 14, 0, 11, 4]]
    grid_pose[:, 2, 2] = src_graph_pose[:, [8, 9]].mean(1)  # midpoint of neck and nose

    grid_pose[:, 3] = src_graph_pose[:, [2, 15, 9, 12, 5]]
    grid_pose[:, 4] = src_graph_pose[:, [3, 16, 10, 13, 6]]

    grid_pose = grid_pose.transpose([0, 3, 1, 2])   # B*C*5*5

    return grid_pose
def inverse_semantic_grid_trans(src_grid_pose:np.ndarray):
    """
    1 x 3 x 5 x 5 -> 1 x 17 x 3
    """
    batch_size, C = src_grid_pose.shape[:2]
    src_grid_pose = src_grid_pose.transpose([0, 2, 3, 1])  # B*5*5*C

    graph_pose = np.zeros([batch_size, 17, C])
    graph_pose[:, 7] = src_grid_pose[:, 0].mean(axis=1)
    graph_pose[:, 0] = src_grid_pose[:, 1, [0, 4]].mean(axis=1)
    graph_pose[:, 8] = src_grid_pose[:, 1, [1, 2, 3]].mean(axis=1)
    graph_pose[:, [1, 14, 11, 4]] = src_grid_pose[:, 2, [0, 1, 3, 4]]
    graph_pose[:, [2, 15, 9, 12, 5]] = src_grid_pose[:, 3]
    graph_pose[:, [3, 16, 10, 13, 6]] = src_grid_pose[:, 4]


    return graph_pose


import pdb

import torch.nn as nn
import torch
import torch.nn.functional as F
import ipdb
import math
import torch.nn.init as init
from collections import Counter
import numpy as np

ReLU = nn.ReLU

class AutoDynamicGridLiftingNetwork(nn.Module):
    def __init__(self,
                 hidden_size=256,
                 num_block=2,
                 num_jts=17,
                 out_num_jts=17,
                 p_dropout=0.25,
                 input_dim=2,
                 output_dim = 3,
                 temperature=30,
                 grid_shape=(5,5),
                 padding_mode=('c','z'),
                 autosgt_prior='standard'):
        super(AutoDynamicGridLiftingNetwork, self).__init__()

        self.linear_size = hidden_size
        self.num_stage = num_block
        self.num_jts = num_jts
        self.out_num_jts = num_jts
        self.inp_dim = input_dim
        self.out_dim = output_dim
        self.p_dropout = p_dropout

        self.input_size = num_jts * input_dim
        self.output_size = out_num_jts * output_dim

        conv3 = TwoBranchDGridConv

        self.w1 = conv3(in_channels=2, out_channels=self.linear_size, kernel_size=3, padding_mode=padding_mode, bias=True)
        self.batch_norm1 = nn.BatchNorm2d(self.linear_size)
        self.dropout = nn.Dropout2d(p=self.p_dropout)

        self.atten_conv1 = DynamicAttention2D(in_planes=input_dim, out_planes=self.linear_size, kernel_size=3,
                                          spatial_size=grid_shape, ratios=1/16., temperature=temperature, groups=1)
        self.linear_stages = []
        for l in range(num_block):
            self.linear_stages.append(CNNBlock(self.linear_size, grid_shape=grid_shape, padding_mode=padding_mode, p_dropout=self.p_dropout, temperature=temperature))
        self.linear_stages = nn.ModuleList(self.linear_stages)

        self.w2 = conv3(in_channels=self.linear_size, out_channels=3, kernel_size=3, padding_mode=padding_mode, bias=False)

        self.atten_conv2 = DynamicAttention2D(in_planes=self.linear_size, out_planes=output_dim, kernel_size=3,
                                              spatial_size=grid_shape, ratios=1/16., temperature=temperature, groups=1)
        self.relu = ReLU(inplace=True)

        self.grid_shape = list(grid_shape)
        self.sgt_layer = AutoSGT(num_jts=num_jts, grid_shape=grid_shape, autosgt_prior=autosgt_prior)

    def net_update_temperature(self, temperature):
        for m in self.modules():
            if hasattr(m, "update_temperature"):
                m.update_temperature(temperature)

    def forward(self, x, gumbel_temp=1.0, use_gumbel_noise=False, is_training=False):
        batch_size = x.shape[0]
        sgt_trans_mat_hard = self.sgt_layer(gumbel_temp=gumbel_temp, use_gumbel_noise=use_gumbel_noise, is_training=is_training).repeat([batch_size, 1, 1])

        x = torch.bmm(sgt_trans_mat_hard, x)
        x = x.reshape([batch_size] + list(self.grid_shape) + [self.inp_dim]).permute([0, 3, 1, 2])       # B*HW*C -> B*C*H*W

        atten1 = self.atten_conv1(x)
        y = self.w1(x, atten1)
        y = self.batch_norm1(y)
        y = self.relu(y)
        y = self.dropout(y)

        for i in range(self.num_stage):
            y = self.linear_stages[i](y)

        atten2 = self.atten_conv2(y)
        y = self.w2(y, atten2)

        y = y.permute([0, 2, 3, 1]).reshape(batch_size, np.prod(self.grid_shape), self.out_dim)     # B*C*H*W -> B*HW*C
        sgt_trans_mat_inverse = sgt_trans_mat_hard.permute([0, 2, 1])
        joint_reweight = sgt_trans_mat_inverse.sum(dim=-1, keepdim=True) + 1e-8
        y = torch.bmm(sgt_trans_mat_inverse, y) / joint_reweight

        return y

class CNNBlock(nn.Module):
    def __init__(self, linear_size, grid_shape, padding_mode, p_dropout=0.25, biased=True, temperature=30):
        super(CNNBlock, self).__init__()
        self.l_size = linear_size

        self.relu = ReLU(inplace=True)
        self.kernel_size = 3
        conv3 = TwoBranchDGridConv

        self.w1 = conv3(in_channels=linear_size, out_channels=linear_size, kernel_size=3, padding_mode=padding_mode, bias=biased)
        self.batch_norm1 = nn.BatchNorm2d(self.l_size)

        self.w2 = conv3(in_channels=linear_size, out_channels=linear_size, kernel_size=3, padding_mode=padding_mode, bias=biased)
        self.batch_norm2 = nn.BatchNorm2d(self.l_size)

        self.atten_conv1 = DynamicAttention2D(in_planes=linear_size, out_planes=linear_size, kernel_size=self.kernel_size,
                                          spatial_size=grid_shape, ratios=1 / 16., temperature=temperature, groups=1)
        self.atten_conv2 = DynamicAttention2D(in_planes=linear_size, out_planes=linear_size, kernel_size=self.kernel_size,
                                          spatial_size=grid_shape, ratios=1 / 16., temperature=temperature, groups=1)

        self.dropout = nn.Dropout2d(p=p_dropout)

    def forward(self, x):
        atten1 = self.atten_conv1(x)
        y = self.w1(x,atten1)
        y = self.batch_norm1(y)
        y = self.relu(y)
        y = self.dropout(y)

        atten2 = self.atten_conv2(y)
        y = self.w2(y,atten2)
        y = self.batch_norm2(y)
        y = self.relu(y)
        y = self.dropout(y)

        out = x + y

        return out

class TwoBranchDGridConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, bias=False, padding_mode=None):
        super(TwoBranchDGridConv, self).__init__()
        self.kernel_size = kernel_size
        self.in_chn = in_channels
        self.out_chn = out_channels
        self.branch1_weight = nn.Parameter(torch.zeros(out_channels, in_channels, self.kernel_size, self.kernel_size))
        self.branch2_weight = nn.Parameter(torch.zeros(out_channels, in_channels, self.kernel_size, self.kernel_size))
        self.has_bias = bias
        self.padding_mode = padding_mode
        if bias:
            self.branch1_bias = nn.Parameter(torch.zeros(out_channels))
            self.branch2_bias = nn.Parameter(torch.zeros(out_channels))
        else:
            self.register_parameter('branch1_bias', None)
            self.register_parameter('branch2_bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        for weight, bias in [[self.branch1_weight, self.branch1_bias], [self.branch2_weight, self.branch2_bias]]:
            init.kaiming_uniform_(weight, a=math.sqrt(5))
            if bias is not None:
                fan_in, _ = init._calculate_fan_in_and_fan_out(weight)
                bound = 1 / math.sqrt(fan_in)
                init.uniform_(bias, -bound, bound)

    def unfolding_conv(self, x_pad, weight, bias, atten):
        kernel_size = self.branch1_weight.shape[-2]
        batch_size, cin, h_pad, w_pad = x_pad.shape
        h = h_pad - kernel_size + 1 # h: 5 h_pad: 7 kernel_size: 3
        w = w_pad - kernel_size + 1
        x_unfold = F.unfold(x_pad, (kernel_size, kernel_size))
        x_unfold_avg_weight = (x_unfold.reshape(batch_size, cin, kernel_size*kernel_size, h*w) * atten.reshape(batch_size, 1, kernel_size*kernel_size, h*w)).reshape(batch_size, cin*kernel_size*kernel_size, h*w)
        out = x_unfold_avg_weight.transpose(1,2).matmul(weight.view(weight.shape[0], -1).t()).transpose(1,2) # B*(C*k*K)*(5*5)
        out_fold = F.fold(out, (5,5), (1,1))
        if bias is not None:
            out_fold = out_fold + bias.reshape(1, -1, 1, 1)

        return out_fold


    def forward(self, x, atten=None):
        padding_kwargs = {
            'c':dict(mode='circular'),
            'z':dict(mode='constant', value=0),
            'r':dict(mode='replicate')
        }
        x_branch1 = F.pad(x, [1, 1, 1, 1], **padding_kwargs[self.padding_mode[0]])
        x_branch2 = F.pad(x, [1, 1, 1, 1], **padding_kwargs[self.padding_mode[1]])

        y_branch1 = self.unfolding_conv(x_branch1, weight=self.branch1_weight, bias=self.branch1_bias, atten=atten)
        y_branch2 = self.unfolding_conv(x_branch2, weight=self.branch2_weight, bias=self.branch2_bias, atten=atten)

        out = y_branch1 + y_branch2

        return out

class DynamicAttention2D(nn.Module):
    def __init__(self, in_planes, out_planes, spatial_size, kernel_size, ratios, temperature, init_weight=True,
                 min_channel=16, groups=1):
        super(DynamicAttention2D, self).__init__()
        self.temperature = temperature
        self.kernel_size = kernel_size
        self.spatial_size = spatial_size
        self.in_planes = in_planes
        self.out_planes = out_planes
        self.groups = groups
        self.attention_channel = max(int(in_planes * ratios), min_channel)

        self.avgpool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Conv2d(in_planes, self.attention_channel, 1, bias=False)
        self.bn = nn.BatchNorm2d(self.attention_channel)
        self.sigmoid = nn.Sigmoid()
        self.relu = nn.ReLU(inplace=True)

        self.position_fc = nn.Conv2d(self.attention_channel, self.kernel_size * self.kernel_size * np.prod(spatial_size), 1, bias=True)


        if init_weight:
            self._initialize_weights()

        self.forward_func = self.forward_vanila

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            if isinstance(m ,nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def update_temperature(self, temperature):
        self.temperature = temperature


    def forward_vanila(self, x):
        x = self.relu(self.bn(self.fc(self.avgpool(x))))

        x = self.position_fc(x).view(x.size(0), self.kernel_size**2, self.spatial_size[0], self.spatial_size[1])
        x = self.sigmoid(x/self.temperature)

        return x

    def forward(self, x):
        return self.forward_func(x)


class AutoSGT(nn.Module):
    def __init__(self, num_jts, grid_shape, autosgt_prior):
        super(AutoSGT, self).__init__()
        self.grid_shape = grid_shape
        self.J = num_jts
        self.HW = np.prod(grid_shape)

        self.register_parameter('sgt_trans_mat', torch.nn.Parameter(self.init_sgt_prior(autosgt_prior)))

    def forward(self, use_gumbel_noise, gumbel_temp, is_training=False):
        sgt_trans_mat = self.sgt_trans_mat
        if self.training:
            if use_gumbel_noise:
                sgt_trans_mat_hard = F.gumbel_softmax(sgt_trans_mat, tau=gumbel_temp, hard=False, dim=-1)
            else:
                dim = -1
                index = sgt_trans_mat.max(dim, keepdim=True)[1]
                y_hard = torch.zeros_like(sgt_trans_mat, memory_format=torch.legacy_contiguous_format).scatter_(dim, index, 1.0)
                sgt_trans_mat_hard = y_hard - sgt_trans_mat.detach() + sgt_trans_mat
        else:
            sgt_trans_mat_hard = F.one_hot(torch.argmax(sgt_trans_mat, -1)).float()

        return sgt_trans_mat_hard
    
    def init_sgt_prior(self, prior_type):
        assert self.J == 17 and self.HW == 25
        if prior_type == 'standard':
            prior_sgt_mat = torch.zeros(self.grid_shape + [self.J])
            # row 0
            prior_sgt_mat[0, :, 7] = 1
            # row 1
            prior_sgt_mat[1, [0, -1], 0] = 1
            prior_sgt_mat[1, [1, 2, 3], 8] = 1
            # row 2
            prior_sgt_mat[2, 0, 4] = 1
            prior_sgt_mat[2, 1, 11] = 1
            prior_sgt_mat[2, 2, 9] = 1
            prior_sgt_mat[2, 3, 14] = 1
            prior_sgt_mat[2, 4, 1] = 1
            # row 3
            prior_sgt_mat[3, 0, 5] = 1
            prior_sgt_mat[3, 1, 12] = 1
            prior_sgt_mat[3, 2, 9] = 1
            prior_sgt_mat[3, 3, 15] = 1
            prior_sgt_mat[3, 4, 2] = 1
            # row 4
            prior_sgt_mat[4, 0, 6] = 1
            prior_sgt_mat[4, 1, 13] = 1
            prior_sgt_mat[4, 2, 10] = 1
            prior_sgt_mat[4, 3, 16] = 1
            prior_sgt_mat[4, 4, 3] = 1
            prior_sgt_mat = prior_sgt_mat.reshape(1, self.HW, self.J)
        elif prior_type == 'learnt_type1':
            prior_sgt_mat = torch.LongTensor([[7,4,7,1,0,
                                              0,8,8,8,0,
                                              4,11,9,14,1,
                                              5,12,9,15,2,
                                              6,13,10,16,3]])
            prior_sgt_mat = F.one_hot(prior_sgt_mat, num_classes=self.J).float()    # 1*self.HW*self.J
        elif prior_type == 'learnt_type2':
            prior_sgt_mat = torch.LongTensor([[0,15,7,1,0,
                                               1,14,8,7,0,
                                               4,0,9,13,1,
                                               2,6,11,10,2,
                                               5,12,14,16,3]])
            prior_sgt_mat = F.one_hot(prior_sgt_mat, num_classes=self.J).float()    # 1*self.HW*self.J
        elif prior_type == 'learnt_type3':
            prior_sgt_mat = torch.LongTensor([[9,7,7,10,7,
                                               13,8,10,15,16,
                                               9,12,7,14,1,
                                               4,5,7,3,11,
                                               7,6,9,2,14]])
            prior_sgt_mat = F.one_hot(prior_sgt_mat, num_classes=self.J).float()    # 1*self.HW*self.J
        elif prior_type == 'random_prob':
            prior_sgt_mat = torch.rand([self.HW, self.J])
            prior_sgt_mat = F.softmax(prior_sgt_mat, dim=-1).unsqueeze(0)
        else:
            raise Exception()

        return prior_sgt_mat

