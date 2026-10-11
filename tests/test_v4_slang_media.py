"""Real bounded ffprobe/ffmpeg preprocessing, synthetic video and scripted model services."""
import asyncio
import shutil
import subprocess
import pytest
from igngbot_v4.slang.media import ModelServices
from igngbot_v4.slang.models import material

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required for optional media smoke; CI/runtime install it')


def test_actual_media_preprocessing_does_not_upgrade_asr_or_visual_inferences(tmp_path):
    video = tmp_path / 'synthetic.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=64x64:d=1',
                    '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono', '-t', '1', '-c:v', 'libx264', '-threads', '1', '-c:a', 'aac', str(video)], check=True, timeout=30)
    calls = []
    async def scenario():
        service = ModelServices({'SLANG_ASR_URL': 'https://example.com/asr', 'SLANG_VISION_URL': 'https://example.com/vision', 'SLANG_VISION_MODEL': 'fixture', 'SLANG_MEDIA_CACHE': str(tmp_path / 'scratch')})
        async def request(url, **kwargs):
            calls.append(url)
            if url.endswith('/asr'):
                return {'segments': [{'start': 0, 'end': 1, 'text': '这是脚本转写结果，不代表音频识别质量。'}]}
            payload = kwargs['payload']
            assert payload['messages'][1]['content'][0]['image_url']['url'].startswith('data:image/jpeg;base64,')
            return {'choices': [{'message': {'content': '合成黑色画面，没有可辨识文字。'}}]}
        service._request = request
        rows = await service.analyze(video.read_bytes(), visual=True)
        snapshot = material({'url': 'https://example.com/synthetic', 'segments': rows})
        assert {r['kind'] for r in rows} == {'asr', 'vision'}
        assert all(not r['verified'] for r in snapshot['segments'])
        assert all(r['end'] <= 1 for r in rows)
        assert not list((tmp_path / 'scratch').iterdir())
    asyncio.run(scenario())
    assert any(url.endswith('/asr') for url in calls) and any(url.endswith('/vision') for url in calls)
