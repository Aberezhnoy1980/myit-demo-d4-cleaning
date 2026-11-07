"""Изолированный декодер: stdin исходник, stdout новый JPEG, без файлов/сети/логов."""
import io
import resource
import sys
import warnings


def main():
    # Ограничения процесса применяются до импорта Pillow и декодирования.
    # Darwin отклоняет RLIMIT_DATA/RLIMIT_AS; RSS контролирует родительский watchdog.
    resource.setrlimit(resource.RLIMIT_CPU, (3, resource.getrlimit(resource.RLIMIT_CPU)[1]))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 20_000_000
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    source = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if not source or len(source) > 5 * 1024 * 1024:
        return 1
    expected = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP'}.get(sys.argv[1])
    if expected is None:
        return 1
    with Image.open(io.BytesIO(source), formats=['JPEG', 'PNG', 'WEBP']) as probe:
        if probe.format != expected or probe.width * probe.height > 20_000_000:
            return 1
        if getattr(probe, 'n_frames', 1) != 1:
            return 1
        probe.verify()
    with Image.open(io.BytesIO(source), formats=['JPEG', 'PNG', 'WEBP']) as original:
        original.load()
        oriented = ImageOps.exif_transpose(original)
        oriented.thumbnail((2000, 2000))
        rgba = oriented.convert('RGBA')
        clean = Image.new('RGB', rgba.size, 'white')
        clean.paste(rgba, mask=rgba.getchannel('A'))
        output = io.BytesIO()
        clean.save(output, format='JPEG', quality=82)  # без EXIF/ICC/XMP и исходного имени
    if len(output.getvalue()) > 5 * 1024 * 1024:
        return 1
    sys.stdout.buffer.write(output.getvalue())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)
