import React from 'react';
import {
  AbsoluteFill,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
  Video,
} from 'remotion';

export interface WhisperWord {
  word: string;
  start: number;
  end: number;
}

export interface StorySceneProps {
  /** narration_text dari script_production.json */
  title: string;
  /** Nama file di public/kra_temp/ (string kosong = scene teks saja) */
  videoSrc: string;
  words: WhisperWord[];
  highlightWords: string[];
  durationInSeconds: number;
  /** Opsional, dipakai composition CreatorOverlay */
  lowerThirdText?: string;
  showLowerThird?: boolean;
  visualCue?: string;
}

export const defaultStoryProps: StorySceneProps = {
  title: 'Judul scene',
  videoSrc: '',
  words: [],
  highlightWords: [],
  durationInSeconds: 5,
  lowerThirdText: '',
  showLowerThird: false,
  visualCue: '',
};

/** Bandingkan kata tanpa tanda baca & huruf besar. */
const normalise = (value: string): string =>
  value.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, '').trim();

const isHighlighted = (word: string, highlights: string[]): boolean => {
  const needle = normalise(word);
  if (!needle) return false;
  return highlights.some((item) => {
    const target = normalise(item);
    return target.length > 0 && (needle.includes(target) || target.includes(needle));
  });
};

/**
 * Chapter 03: scene narasi dengan caption per kata dari Whisper.
 *
 * Catatan penting soal `staticFile()`: Chromium headless milik Remotion tidak
 * boleh membuka `file://`, jadi video harus berada di `public/kra_temp/`.
 * `studio/story.py` menyalin aset ke sana sebelum render dan membersihkannya
 * setelah semua scene selesai.
 */
export const StoryScene: React.FC<StorySceneProps> = ({
  title,
  videoSrc,
  words,
  highlightWords,
  lowerThirdText,
  showLowerThird,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentTime = frame / fps;

  // Kata yang sedang diucapkan; kalau ada jeda pendek, tahan kata terakhir
  // supaya caption tidak berkedip hilang di antara dua kata.
  let activeWord = '';
  let highlighted = false;
  for (let index = 0; index < words.length; index += 1) {
    const word = words[index];
    if (currentTime >= word.start && currentTime <= word.end) {
      activeWord = word.word.trim();
      highlighted = isHighlighted(activeWord, highlightWords || []);
      break;
    }
    if (currentTime > word.end && currentTime - word.end < 0.25) {
      activeWord = word.word.trim();
      highlighted = isHighlighted(activeWord, highlightWords || []);
    }
  }

  const pop = interpolate(frame % Math.max(1, Math.round(fps * 0.4)), [0, 6], [0.94, 1], {
    extrapolateRight: 'clamp',
  });

  return (
    <AbsoluteFill style={{ backgroundColor: '#0f0f14' }}>
      {videoSrc ? (
        <Video
          src={staticFile(`kra_temp/${videoSrc}`)}
          style={{ width: '100%', height: '100%', objectFit: 'cover' }}
        />
      ) : (
        <AbsoluteFill
          style={{
            background: 'radial-gradient(circle at 30% 30%, #1b1440 0%, #0f0f14 70%)',
          }}
        />
      )}

      {showLowerThird && lowerThirdText ? (
        <AbsoluteFill style={{ justifyContent: 'flex-start', alignItems: 'flex-start', padding: 64 }}>
          <div
            style={{
              fontFamily: 'Montserrat, sans-serif',
              fontSize: 34,
              fontWeight: 700,
              color: '#0f0f14',
              backgroundColor: '#FFE600',
              padding: '10px 24px',
              borderRadius: 8,
              letterSpacing: 0.5,
            }}
          >
            {lowerThirdText}
          </div>
        </AbsoluteFill>
      ) : null}

      {activeWord ? (
        <AbsoluteFill
          style={{ justifyContent: 'flex-end', alignItems: 'center', paddingBottom: 120 }}
        >
          <div
            style={{
              fontFamily: 'Montserrat, sans-serif',
              fontSize: highlighted ? 78 : 64,
              fontWeight: 900,
              color: highlighted ? '#FFE600' : '#FFFFFF',
              textTransform: 'uppercase',
              textShadow: '0px 6px 24px rgba(0,0,0,0.9)',
              backgroundColor: 'rgba(0,0,0,0.55)',
              padding: '12px 32px',
              borderRadius: 16,
              transform: `scale(${pop})`,
            }}
          >
            {activeWord}
          </div>
        </AbsoluteFill>
      ) : null}

      {title ? (
        <AbsoluteFill style={{ opacity: 0 }} aria-label={title} />
      ) : null}
    </AbsoluteFill>
  );
};
