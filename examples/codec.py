import argparse
import struct
import sys
import time

from pathlib import Path

import torch

from torchvision.transforms import ToPILImage, ToTensor

from PIL import Image

import compressai
from compressai.zoo import models

model_ids = {k: i for i, k in enumerate(models.keys())}

metric_ids = {
    'mse': 0,
}

def inverse_dict(d):
    # We assume dict values are unique...
    assert len(d.keys()) == len(set(d.keys()))
    return {v: k for k, v in d.items()}


def filesize(filepath: str) -> int:
    if not Path(filepath).is_file():
        raise ValueError(f'Invalid file "{filepath}".')
    return Path(filepath).stat().st_size


def load_image(filepath: str) -> Image.Image:
    return Image.open(filepath).convert('RGB')


def img2torch(img: Image.Image) -> torch.Tensor:
    return ToTensor()(img).unsqueeze(0)


def torch2img(x: torch.Tensor) -> Image.Image:
    return ToPILImage()(x.clamp_(0, 1).squeeze())


def write_uints(fd, values, fmt='>{:d}I'):
    fd.write(struct.pack(fmt.format(len(values)), *values))


def write_uchars(fd, values, fmt='>{:d}B'):
    fd.write(struct.pack(fmt.format(len(values)), *values))


def read_uints(fd, n, fmt='>{:d}I'):
    sz = struct.calcsize('I')
    return struct.unpack(fmt.format(n), fd.read(n * sz))


def read_uchars(fd, n, fmt='>{:d}B'):
    sz = struct.calcsize('B')
    return struct.unpack(fmt.format(n), fd.read(n * sz))


def write_bytes(fd, values, fmt='>{:d}s'):
    if len(values) == 0:
        return
    fd.write(struct.pack(fmt.format(len(values)), values))


def read_bytes(fd, n, fmt='>{:d}s'):
    sz = struct.calcsize('s')
    return struct.unpack(fmt.format(n), fd.read(n * sz))[0]


def get_header(model_name, metric, quality):
    """Format header information:
    - 1 byte for model id
    - 4 bits for metric
    - 4 bits for quality param
    """
    metric = metric_ids[metric]
    code = (metric << 4) | (quality - 1 & 0x0F)
    return model_ids[model_name], code


def parse_header(header):
    """Read header information from 2 bytes:
    - 1 byte for model id
    - 4 bits for metric
    - 4 bits for quality param
    """
    model_id, code = header
    quality = (code & 0x0F) + 1
    metric = code >> 4
    return (inverse_dict(model_ids)[model_id],
            inverse_dict(metric_ids)[metric], quality)


def _encode(image, model, metric, quality, coder, output):
    compressai.set_entropy_coder(coder)
    enc_start = time.time()

    img = load_image(image)
    start = time.time()
    net = models[model](quality=quality, metric=metric, pretrained=True).eval()
    load_time = time.time() - start

    with torch.no_grad():
        x = img2torch(img)
        out = net.compress(x)

    shape = out['shape']
    header = get_header(model, metric, quality)

    with Path(output).open('wb') as f:
        write_uchars(f, header)
        # write shape and number of encode latents
        write_uints(f, (shape[0], shape[1], len(out['strings'])))
        for s in out['strings']:
            write_uints(f, (len(s[0]), ))
            write_bytes(f, s[0])

    enc_time = time.time() - enc_start
    size = filesize(output)
    bpp = float(size) * 8 / (img.size[0] * img.size[1])
    print(f'{bpp:.3f} bpp |'
          f' Encoded in {enc_time:.2f}s (model loading: {load_time:.2f}s)')


def _decode(inputpath, coder, show, output=None):
    compressai.set_entropy_coder(coder)

    dec_start = time.time()
    with Path(inputpath).open('rb') as f:
        model, metric, quality = parse_header(read_uchars(f, 2))
        shape = read_uints(f, 2)
        strings = []
        n_strings = read_uints(f, 1)[0]
        for _ in range(n_strings):
            s = read_bytes(f, read_uints(f, 1)[0])
            strings.append([s])

    print(f'Model: {model:s}, metric: {metric:s}, quality: {quality:d}')
    start = time.time()
    net = models[model](quality=quality, metric=metric, pretrained=True).eval()
    load_time = time.time() - start

    with torch.no_grad():
        out = net.decompress(shape, strings)
    img = torch2img(out['x_hat'])
    dec_time = time.time() - dec_start
    print(f'Decoded in {dec_time:.2f}s (model loading: {load_time:.2f}s)')

    if show:
        show_image(img)
    if output is not None:
        img.save(output)


def show_image(img: Image.Image):
    from matplotlib import pyplot as plt
    fig, ax = plt.subplots()
    ax.axis('off')
    ax.title.set_text('Decoded image')
    ax.imshow(img)
    fig.tight_layout()
    plt.show()


def encode(argv):
    parser = argparse.ArgumentParser(description='Encode image to bit-stream')
    parser.add_argument('image', type=str)
    parser.add_argument('--model',
                        choices=models.keys(),
                        default=list(models.keys())[0],
                        help='NN model to use (default: %(default)s)')
    parser.add_argument('-m',
                        '--metric',
                        choices=['mse'],
                        default='mse',
                        help='metric trained against (default: %(default)s')
    parser.add_argument('-q',
                        '--quality',
                        choices=list(range(1, 9)),
                        type=int,
                        default=3,
                        help='Quality setting (default: %(default)s)')
    parser.add_argument('-c',
                        '--coder',
                        choices=compressai.available_entropy_coders(),
                        default=compressai.available_entropy_coders()[0],
                        help='Entropy coder (default: %(default)s)')
    parser.add_argument('-o', '--output', help='Output path')
    args = parser.parse_args(argv)
    if not args.output:
        args.output = Path(Path(args.image).resolve().name).with_suffix('.bin')
    _encode(args.image, args.model, args.metric, args.quality, args.coder,
            args.output)


def decode(argv):
    parser = argparse.ArgumentParser(description='Decode bit-stream to imager')
    parser.add_argument('input', type=str)
    parser.add_argument('-c',
                        '--coder',
                        choices=compressai.available_entropy_coders(),
                        default=compressai.available_entropy_coders()[0],
                        help='Entropy coder (default: %(default)s)')
    parser.add_argument('--show', action='store_true')
    parser.add_argument('-o', '--output', help='Output path')
    args = parser.parse_args(argv)
    _decode(args.input, args.coder, args.show, args.output)


def parse_args(argv):
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('command', choices=['encode', 'decode'])
    args = parser.parse_args(argv)
    return args


def main(argv):
    args = parse_args(argv[1:2])
    argv = argv[2:]
    torch.set_num_threads(1)  # just to be sure
    if args.command == 'encode':
        encode(argv)
    elif args.command == 'decode':
        decode(argv)


if __name__ == '__main__':
    main(sys.argv)