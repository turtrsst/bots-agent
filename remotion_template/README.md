# Template Remotion untuk studio AutoGen

Salin dua file ini ke repo Remotion KRA:

```bash
cp remotion_template/Root.tsx        ../kantor-rama-ai/remotion_studio/src/Root.tsx
cp remotion_template/StoryScene.tsx  ../kantor-rama-ai/remotion_studio/src/StoryScene.tsx
mkdir -p ../kantor-rama-ai/remotion_studio/public/kra_temp
```

Pastikan `src/index.ts` (entry Remotion) sudah mendaftarkan root:

```ts
import { registerRoot } from 'remotion';
import { RemotionRoot } from './Root';

registerRoot(RemotionRoot);
```

Lalu set di `.env`:

```env
STUDIO_REMOTION_DIR=/path/ke/kantor-rama-ai/remotion_studio
STUDIO_REMOTION_COMPOSITION=StoryScene
```

## Kenapa dua file ini

| Masalah | Solusi di template |
|---|---|
| Chromium Remotion diblokir saat membuka `file://` | `staticFile('kra_temp/<file>')`; `studio/story.py` menyalin aset ke `public/kra_temp/` lalu menghapusnya setelah render |
| Durasi komposisi statis | `calculateMetadata` menghitung `durationInFrames` dari `durationInSeconds` (hasil Whisper) |
| Caption per kata berkedip | kata terakhir "ditahan" 0.25 s sampai kata berikutnya |
| `composition_id` berbeda per scene (`StoryScene`, `CreatorOverlay`) | keduanya didaftarkan dan memakai props yang sama |
| Highlight dari Clara (`highlight_words`) | dicocokkan tanpa huruf besar/tanda baca |

## Props yang dikirim `studio/story.py`

```json
{
  "title": "Pernahkah kamu membayangkan kantor yang berjalan sendiri?",
  "videoSrc": "scene_1_hook__broll_server.mp4",
  "words": [{"word": "kantor", "start": 0.42, "end": 0.88}],
  "highlightWords": ["berjalan sendiri", "tidur"],
  "durationInSeconds": 7.31,
  "lowerThirdText": "Rama - Founder KRA",
  "visualCue": "Dark futuristic server room with glowing neon nodes"
}
```

File props ditulis ke `workspace/props_<nn>_<scene_id>.json` supaya bisa diperiksa
manual kalau render mencurigakan.
