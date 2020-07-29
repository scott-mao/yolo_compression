import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import OrderedDict
ONNX_EXPORT = False

class weightedFeatureFusion(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers, weight=False):
        super(weightedFeatureFusion, self).__init__()
        self.layers = layers  # layer indices
        self.weight = weight  # apply weights boolean
        self.n = len(layers) + 1 # number of layers
        if weight:
            self.w = torch.nn.Parameter(torch.zeros(self.n))  # layer weights

    def forward(self, x, outputs):
        # Weights
        if self.weight:
            w = torch.sigmoid(self.w) * (2 / self.n)  # sigmoid weights (0-1)
            x = x * w[0]

        # Fusion
        nc = x.shape[1]  # input of channels
        for i in range(self.n - 1):
            a = outputs[self.layers[i]] * w[i + 1] if self.weight else outputs[self.layers[i]]  # feature to add
            ac = a.shape[1] # feature channels
            dc = nc - ac # delta channels

            # Adjust channels
            if dc > 0:  # slice input
                x[:, :ac] = x[:, :ac] + a  # or a = nn.ZeroPad2d((0, 0, 0, 0, 0, dc))(a); x = x + a
            elif dc < 0:  # slice feature
                x = x + a[:, :nc]
            else:  # same shape
                x = x + a
        return x


class SwishImplementation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, i):
        ctx.save_for_backward(i)
        return i * torch.sigmoid(i)

    @staticmethod
    def backward(ctx, grad_output):
        sigmoid_i = torch.sigmoid(ctx.saved_variables[0])
        return grad_output * (sigmoid_i * (1 + ctx.saved_variables[0] * (1 - sigmoid_i)))


class MemoryEfficientSwish(nn.Module):
    def forward(self, x):
        return SwishImplementation.apply(x)


class Swish(nn.Module):
    def forward(self, x):
        return x.mul_(torch.sigmoid(x))


class Mish(nn.Module):  # https://github.com/digantamisra98/Mish
    def forward(self, x):
        return x.mul_(F.softplus(x).tanh())


################
# My Additions #
################
class MultiBiasConv(nn.Module):
    def __init__(self, in_channels, out_channels, n_bias, kernel_size=(3, 3), stride=1, pad=0):
        super(MultiBiasConv, self).__init__()
        self.out_channels = out_channels
        self.conv = nn.Conv2d(in_channels=int(in_channels), out_channels=int(out_channels/n_bias), bias=False, kernel_size=kernel_size, padding=pad, stride=stride)
        nn.init.xavier_normal_(self.conv.weight)
        self.bias = torch.nn.Parameter( torch.Tensor(n_bias), requires_grad=True )
        nn.init.normal_(self.bias)
    
    def forward(self, x):
        x = self.conv(x)
        y = []
        for b in self.bias:
            y.append(x + b)
        
        return torch.cat(y, dim=1)


class MultiConvMultiBias(nn.Module):
    def __init__(self, in_channels, out_channels, n_bias, kernel_size=3, stride=1, pad=0):
        super(MultiConvMultiBias, self).__init__()
        # Input is not divisible by 2^n
        bias_1, bias_2 = n_bias, n_bias
        if in_channels == 3:
            bias_1 = 1
        self.conv1 = MultiBiasConv(in_channels=in_channels, out_channels=in_channels, n_bias=bias_1, 
            kernel_size=(kernel_size, 1), pad=pad, stride=stride
        )
        self.conv2 = MultiBiasConv(in_channels=in_channels, out_channels=out_channels, n_bias=bias_2, 
            kernel_size=(1, kernel_size), pad=pad, stride=stride
        )
    
    def forward(self, x):
        x = F.leaky_relu(self.conv1(x))
        x = self.conv2(x)
        return x


class HalfConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, pad, have_bias):
        super(HalfConv, self).__init__()
        self.conv = nn.Conv2d(
            in_channels=in_channels, out_channels=int(out_channels/2),
            kernel_size=kernel_size, stride = stride, padding=pad, bias=have_bias
        )
        

    def forward(self, x):
        x = self.conv(x)
        x = torch.cat((x, -x), 1)

        return x


class MyInception(nn.Module):

    def __init__(self, n_in, n_out):
        super(MyInception, self).__init__()

        self.conv1x1 = nn.Conv2d(in_channels=n_in, out_channels=int(n_out/2), kernel_size=1, stride=1, padding=0)
        self.conv1x1_3x3 = nn.Sequential(
            nn.Conv2d(in_channels=n_in, out_channels=int(n_out/4), kernel_size=1, stride=1, padding=0),
            nn.Conv2d(in_channels=int(n_out/4), out_channels=int(n_out/4), kernel_size=3, stride=1, padding=1)
        )
        self.conv1x1_3x3_3x3 = nn.Sequential(
            nn.Conv2d(in_channels=n_in, out_channels=int(n_out/4), kernel_size=1, stride=1, padding=0),
            nn.Conv2d(in_channels=int(n_out/4), out_channels=int(n_out/4), kernel_size=3, stride=1, padding=1),
            nn.Conv2d(in_channels=int(n_out/4), out_channels=int(n_out/4), kernel_size=3, stride=1, padding=1)
        )
    
    def forward(self, x):
        x1 = self.conv1x1(x)
        x2 = self.conv1x1_3x3(x)
        x3 = self.conv1x1_3x3_3x3(x)

        return torch.cat((x1, x2, x3), 1)


def sigmoid(x):
    return float(1./(1.+torch.exp(-x)))


class SoftMaskedConv2d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, padding=1, stride=1, mask_initial_value=0.):
        super(SoftMaskedConv2d, self).__init__()
        self.mask_initial_value = mask_initial_value
        
        self.in_channels = in_channels
        self.out_channels = out_channels    
        self.kernel_size = kernel_size
        self.padding = padding
        self.stride = stride
        
        self.weight = nn.Parameter(torch.Tensor(out_channels, in_channels, kernel_size, kernel_size))
        nn.init.xavier_normal_(self.weight)
        self.init_weight = nn.Parameter(torch.zeros_like(self.weight), requires_grad=False)
        self.init_mask()
        
    def init_mask(self):
        self.mask_weight = nn.Parameter(torch.Tensor(self.out_channels, self.in_channels, self.kernel_size, self.kernel_size))
        nn.init.constant_(self.mask_weight, self.mask_initial_value)

    def compute_mask(self, temp, ticket):
        scaling = 1. / sigmoid(self.mask_initial_value)
        if ticket: mask = (self.mask_weight > 0).float()
        else: mask = F.sigmoid(temp * self.mask_weight)
        return scaling * mask      
        
    def prune(self, temp):
        self.mask_weight.data = torch.clamp(temp * self.mask_weight.data, max=self.mask_initial_value)   

    def forward(self, x, temp=1, ticket=False):
        self.mask = self.compute_mask(temp, ticket)
        masked_weight = self.weight * self.mask
        out = F.conv2d(x, masked_weight, stride=self.stride, padding=self.padding)        
        return out
        
    def checkpoint(self):
        self.init_weight.data = self.weight.clone()       
        
    def rewind_weights(self):
        self.weight.data = self.init_weight.clone()

    def extra_repr(self):
        return '{}, {}, kernel_size={}, stride={}, padding={}'.format(
            self.in_channels, self.out_channels, self.kernel_size, self.stride, self.padding)


# Adapted from https://github.com/liux0614/yolo_nano/blob/master/models/yolo_nano.py
def conv1x1(input_channels, output_channels, stride=1, bn=True, bias=False, activation='ReLU6'):
    act_ftn = None
    if activation == 'ReLU6': act_ftn = nn.ReLU6(inplace=True)
    elif activation == 'LeakyReLU': act_ftn = nn.LeakyReLU(0.1, inplace=True)
    assert(activation == 'LeakyReLU' or activation == 'ReLU6')
    
    # 1x1 convolution without padding
    if bn == True:
        return nn.Sequential(
            nn.Conv2d(
                input_channels, output_channels, kernel_size=1,
                stride=stride, bias=bias),
            nn.BatchNorm2d(output_channels),
            act_ftn
        )
    else:
        return nn.Conv2d(
                input_channels, output_channels, kernel_size=1,
                stride=stride, bias=bias)


def conv3x3(input_channels, output_channels, stride=1, bn=True, activation='ReLU6'):
    act_ftn = None
    if activation == 'ReLU6': act_ftn = nn.ReLU6(inplace=True)
    elif activation == 'LeakyReLU': act_ftn = nn.LeakyReLU(0.1, inplace=True)
    assert(activation == 'LeakyReLU' or activation == 'ReLU6')
    
    # 3x3 convolution with padding=1
    if bn == True:
        return nn.Sequential(
            nn.Conv2d(
                input_channels, output_channels, kernel_size=3,
                stride=stride, padding=1, bias=False),
            nn.BatchNorm2d(output_channels),
            act_ftn
        )
    else:
        nn.Conv2d(
                input_channels, output_channels, kernel_size=3,
                stride=stride, padding=1, bias=False)


def sepconv3x3(input_channels, output_channels, stride=1, expand_ratio=1, activation='ReLU6'):
    
    act_ftn = None
    if activation == 'ReLU6': act_ftn = nn.ReLU6(inplace=True)
    elif activation == 'LeakyReLU': act_ftn = nn.LeakyReLU(0.1, inplace=True)
    assert(activation == 'LeakyReLU' or activation == 'ReLU6')
    
    return nn.Sequential(
        # pw
        nn.Conv2d(
            input_channels, input_channels * expand_ratio,
            kernel_size=1, stride=1, bias=False),
        nn.BatchNorm2d(input_channels * expand_ratio),
        act_ftn,
        # dw
        nn.Conv2d(
            input_channels * expand_ratio, input_channels * expand_ratio, kernel_size=3, 
            stride=stride, padding=1, groups=input_channels * expand_ratio, bias=False),
        nn.BatchNorm2d(input_channels * expand_ratio),
        act_ftn,
        # pw-linear
        nn.Conv2d(
            input_channels * expand_ratio, output_channels,
            kernel_size=1, stride=1, bias=False),
        nn.BatchNorm2d(output_channels)
    )


class EP(nn.Module):
    def __init__(self, input_channels, output_channels, stride=1, activation='ReLU6'):
        super(EP, self).__init__()
        self.input_channels = input_channels
        self.output_channels = output_channels
        self.stride = stride
        self.use_res_connect = self.stride == 1 and input_channels == output_channels

        self.sepconv = sepconv3x3(input_channels, output_channels, stride=stride, activation=activation)
        
    def forward(self, x):
        if self.use_res_connect:
            return x + self.sepconv(x)
        
        return self.sepconv(x)


class PEP(nn.Module):
    def __init__(self, input_channels, output_channels, x, stride=1, activation='ReLU6'):
        super(PEP, self).__init__()
        self.input_channels = input_channels
        self.output_channels = output_channels
        self.stride = stride
        self.use_res_connect = self.stride == 1 and input_channels == output_channels

        self.conv = conv1x1(input_channels, x, activation=activation)
        self.sepconv = sepconv3x3(x, output_channels, stride=stride, activation=activation)
        
    def forward(self, x):        
        out = self.conv(x)
        out = self.sepconv(out)
        if self.use_res_connect:
            return out + x

        return out


class FCA(nn.Module):
    def __init__(self, channels, reduction_ratio, activation='ReLU6'):
        super(FCA, self).__init__()
        self.channels = channels
        self.reduction_ratio = reduction_ratio

        act_ftn = None
        if activation == 'ReLU6': act_ftn = nn.ReLU6(inplace=True)
        elif activation == 'LeakyReLU': act_ftn = nn.LeakyReLU(0.1, inplace=True)
        assert(activation == 'LeakyReLU' or activation == 'ReLU6')

        hidden_channels = channels // reduction_ratio
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channels, hidden_channels, bias=False),
            act_ftn,
            nn.Linear(hidden_channels, channels, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        out = self.avg_pool(x).view(b, c)
        out = self.fc(out).view(b, c, 1, 1)
        out = x * out.expand_as(x)
        return out


class SparseConv(nn.Module):

    def __init__(self, original_conv):
        super(SparseConv, self).__init__()
        self.find_non_null_filters(original_conv)
        dict = self.create_splited_convs(original_conv)
        self.fractional_convs = nn.ModuleDict(dict)
    

    def forward(self, x):
        y = []
        # y2 = []
        for (key, value) in self.fractional_convs.items():
            y.append(value(x))
        # for i in reversed(y): y2.append(i)

        return torch.cat(y, dim=1)
        

    def find_non_null_filters(self, conv): # conv.shape is out_channels, in_channels, x, y
        device = next(conv.parameters()).device
        if type(conv) is SoftMaskedConv2d: params = conv.weight * conv.mask
        else: params = conv.weight
        onehot_parameters = torch.sum(torch.abs(params), dim=(1, 2, 3))
        self.convs_list = torch.where( onehot_parameters > 0, torch.tensor(1, device=device), torch.tensor(0, device=device) )
        

    def create_splited_convs(self, original_conv):
        sequential_ones = []
        sequential_zeros = []
        result = OrderedDict()
        count_ones = 1
        count_zeros = 1

        for i in range(self.convs_list.shape[0]):
            if self.convs_list[i] == torch.tensor(1): 
                if len(sequential_zeros ) > 0:
                    new_conv = ZeroConv(sequential_zeros, original_conv.kernel_size, original_conv.padding, original_conv.stride)
                    result['zero' + str(count_zeros)] = new_conv
                    count_zeros += 1
                    sequential_zeros = []

                sequential_ones.append(i)
            else:
                if len(sequential_ones) > 0:
                    new_conv = self.create_miniconv_from(original_conv, sequential_ones)
                    result['conv' + str(count_ones)] = new_conv
                    count_ones += 1
                    sequential_ones = []
                
                sequential_zeros.append(1)
        
        if len(sequential_ones) > 0:
            new_conv = self.create_miniconv_from(original_conv, sequential_ones)
            result['conv' + str(count_ones)] = new_conv
        elif len(sequential_zeros) > 0:
            new_conv = ZeroConv(sequential_zeros, original_conv.kernel_size, original_conv.padding, original_conv.stride)
            result['zero' + str(count_zeros)] = new_conv
        
        return result


    def create_miniconv_from(self, original_conv, channels_list):
        if type(original_conv) == SoftMaskedConv2d:
            new_conv = nn.Conv2d(
                in_channels=original_conv.in_channels, out_channels=len(channels_list), 
                kernel_size=original_conv.kernel_size, padding=original_conv.padding,
                stride=original_conv.stride,
                bias = False
            )
            data = original_conv.weight * original_conv.mask
            new_conv.weight.data = data[ channels_list[0] : channels_list[-1]+1 ]
        else:
            new_conv = nn.Conv2d(
                in_channels=original_conv.in_channels, out_channels=len(channels_list), 
                kernel_size=original_conv.kernel_size, padding=original_conv.padding,
                stride=original_conv.stride, groups=original_conv.groups,
                bias = True if original_conv.bias is not None else False
            )
            new_conv.weight.data = original_conv.weight[ channels_list[0] : channels_list[-1]+1 ]
            if original_conv.bias is not None:
                new_conv.bias.data = original_conv.bias[ channels_list[0] : channels_list[-1]+1 ]

        return new_conv


class ZeroConv(nn.Module):

    def __init__(self, channels_list, kernel_size, padding, stride):
        super(ZeroConv, self).__init__()    
        self.channels = len(channels_list)
        self.kernel = kernel_size if (isinstance(kernel_size, list) or isinstance(kernel_size, tuple)) else [kernel_size, kernel_size]
        self.padding = padding if (isinstance(padding, list) or isinstance(padding, tuple)) else [padding, padding]
        self.stride = stride if (isinstance(stride, list) or isinstance(stride, tuple)) else [stride, stride]
    
    
    def forward(self, input):
        dvc = input.get_device() if input.is_cuda else 'cpu'
        batch_size = input.shape[0]
        width, height = input.shape[-2:]
        width = self.compute_size(width, self.kernel[0], self.padding[0], self.stride[0])
        height = self.compute_size(height, self.kernel[1], self.padding[1], self.stride[1])
        
        return torch.zeros(torch.Size([batch_size, self.channels, width, height]), device=dvc)
    

    def compute_size(self, dimension, kernel, padding, stride):
        return int( ( (dimension - kernel + 2 * padding) / stride) + 1)


class View(nn.Module):
    def __init__(self):
        super(View, self).__init__()

    def forward(self, x): # [Batch-size, flatten]
        return x.view(x.shape[0], -1) 