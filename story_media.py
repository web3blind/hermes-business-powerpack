"""Bounded, profile-local story media preparation."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from PIL import Image, ImageOps

MAX_SOURCE = 40 * 1024 * 1024
ALLOWED = ('image_cache', 'video_cache', 'cache/images', 'cache/videos',
           'business-powerpack/story-media')


def source_path(home: Path, raw: str) -> Path:
    from agent.file_safety import raise_if_read_blocked
    if not isinstance(raw, str) or not raw or '://' in raw:
        raise ValueError('Use a local cached media file')
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError('Use an absolute cached media path')
    resolved = path.resolve(strict=True)
    roots = [home / name for name in ALLOWED]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError('Media must be in this profile cache')
    raise_if_read_blocked(str(resolved))
    if not resolved.is_file() or not 0 < resolved.stat().st_size <= MAX_SOURCE:
        raise ValueError('Media size or type is invalid')
    return resolved


def _run(argv, timeout):
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, check=True,
                              stdin=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin'},
                              close_fds=True).stdout
    except (subprocess.SubprocessError, OSError):
        raise ValueError('Media cannot be prepared') from None


def demuxer(path):
    with path.open('rb') as source:
        header = source.read(16)
    if header[4:8] == b'ftyp':
        return 'mov'
    if header[:4] == b'\x1aE\xdf\xa3':
        return 'matroska'
    if header[:4] == b'RIFF' and header[8:12] == b'AVI ':
        return 'avi'
    raise ValueError('Use an MP4, WebM, MKV or AVI video file')


def probe(path):
    data = _run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                 '-f', demuxer(path), '-show_entries',
                 'format=duration:stream=codec_type,codec_name,width,height',
                 '-of', 'json', str(path)], 8)
    try:
        info = json.loads(data)
        streams = info['streams']
        video = next(s for s in streams if s.get('codec_type') == 'video')
        duration = float(info['format']['duration'])
        return video, duration
    except (KeyError, StopIteration, ValueError, TypeError):
        raise ValueError('Invalid video') from None


def media_directory(home: Path):
    directory = home / 'business-powerpack' / 'story-media'
    if directory.resolve() != directory:
        raise ValueError('Story media directory must not be a symlink')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def prepare(home: Path, raw: str):
    path = source_path(home, raw)
    directory = media_directory(home)
    try:
        with Image.open(path) as original:
            if original.format in {'JPEG', 'PNG', 'WEBP'}:
                if original.width * original.height > 50_000_000:
                    raise ValueError('Image too large')
                image = ImageOps.exif_transpose(original).convert('RGB')
                image = ImageOps.contain(image, (1080, 1920), Image.Resampling.LANCZOS)
                canvas = Image.new('RGB', (1080, 1920), (0, 0, 0))
                canvas.paste(image, ((1080-image.width)//2, (1920-image.height)//2))
                fd, name = tempfile.mkstemp(suffix='.jpg', dir=directory)
                os.close(fd)
                canvas.save(name, 'JPEG', quality=88, optimize=True)
                if Path(name).stat().st_size > 10 * 1024 * 1024:
                    Path(name).unlink(missing_ok=True)
                    raise ValueError('Photo exceeds 10 MB')
                return Path(name), 'photo'
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) in {'Image too large', 'Photo exceeds 10 MB'}:
            raise
    video, duration = probe(path)
    if (not 0 < duration <= 60 or video.get('width', 0) <= 0 or video.get('height', 0) <= 0
            or video['width'] * video['height'] > 50_000_000):
        raise ValueError('Video must be at most 60 seconds')
    fd, name = tempfile.mkstemp(suffix='.mp4', dir=directory)
    os.close(fd)
    output = Path(name)
    try:
        _run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
              '-f', demuxer(path), '-i', str(path), '-map', '0:v:0',
              '-map', '0:a:0?', '-vf', 'scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=720:1280:(ow-iw)/2:(oh-ih)/2',
              '-c:v', 'libx265', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-threads', '2',
              '-x265-params', 'pools=2:frame-threads=2:keyint=30:min-keyint=30:scenecut=0:log-level=error',
              '-r', '30', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
              '-y', str(output)], 100)
        encoded, encoded_duration = probe(output)
        if (encoded.get('codec_name') != 'hevc' or encoded.get('width') != 720
                or encoded.get('height') != 1280 or encoded_duration > 60.5
                or output.stat().st_size > 30 * 1024 * 1024):
            raise ValueError('Prepared video is invalid')
        keys = _run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                     '-f', 'mov', '-skip_frame', 'nokey', '-select_streams',
                     'v:0', '-show_entries', 'frame=best_effort_timestamp_time',
                     '-of', 'json', str(output)], 8)
        timestamps = [float(frame['best_effort_timestamp_time']) for frame in json.loads(keys)['frames']]
        if not timestamps or timestamps[0] > 0.1 or any(b-a > 1.1 for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError('Prepared video keyframes are invalid')
        return output, 'video'
    except BaseException:
        output.unlink(missing_ok=True)
        raise
