import React from 'react';
import {
  AbsoluteFill,
  Audio,
  Img,
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
  /** Video A-Roll Creator atau B-Roll, ada di public/kra_temp/ */
  videoSrc?: string;
  /** Suara narasi TTS (mode faceless), ada di public/kra_temp/ */
  audioSrc?: string;
  /** Gambar latar (Ken Burns) bila tidak ada video */
  bgImageSrc?: string;
  words: WhisperWord[];
  highlightWords: string[];
  durationInSeconds: number;
  lowerThirdText?: string;
  showLowerThird?: boolean;
  visualCue?: string;
}

export const defaultStoryProps: StorySceneProps = {
  title: 'Judul scene',
  videoSrc: '',
  audioSrc: '',
  bgImageSrc: '',
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
 * Satu scene cerita: video/gambar latar + narasi (audio asli atau TTS) + caption
 * per kata dari Whisper.
 *
 * `staticFile('kra_temp/...')` wajib karena Chromium headless Remotion menolak
 * `file://`. `studio/story.py` menyalin semua aset ke `public/kra_temp/` sebelum
 * render dan membersihkannya setelah selesai.
 */
export const StoryScene: React.FC<StorySceneProps> = ({
  videoSrc,
  audioSrc,
  bgImageSrc,
  words,
  highlightWords,
  lowerThirdText,
  showLowerThird,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const currentTime = frame / fps;

  // Ken Burns halus untuk latar statis / B-Roll saat narasi TTS berjalan.
  const scale = interpolate(frame, [0, Math.max(1, durationInFrames)], [1, 1.1], {
    extrapolateRight: 'clamp',
  });

  // Kata yang sedang diucapkan; tahan 0.25 s supaya caption tidak berkedip.
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

  return (
    <AbsoluteFill style={{ backgroundColor: '#0b0c10', overflow: 'hidden' }}>
      {videoSrc ? (
        <Video
          src={staticFile(`kra_temp/${videoSrc}`)}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            transform: audioSrc ? `scale(${scale})` : 'none',
          }}
          muted={Boolean(audioSrc)}
        />
      ) : null}

      {!videoSrc && bgImageSrc ? (
        <Img
          src={staticFile(`kra_temp/${bgImageSrc}`)}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            transform: `scale(${scale})`,
          }}
        />
      ) : null}

      {!videoSrc && !bgImageSrc ? (
        <AbsoluteFill
          style={{
            background: 'radial-gradient(circle at 30% 30%, #1b1440 0%, #0b0c10 70%)',
          }}
        />
      ) : null}

      {audioSrc ? <Audio src={staticFile(`kra_temp/${audioSrc}`)} /> : null}

      {showLowerThird && lowerThirdText ? (
        <AbsoluteFill style={{ justifyContent: 'flex-start', alignItems: 'flex-start', padding: 64 }}>
          <div
            style={{
              fontFamily: 'Montserrat, sans-serif',
              fontSize: 34,
              fontWeight: 700,
              color: '#0b0c10',
              backgroundColor: '#FFE600',
              padding: '10px 24px',
              borderRadius: 8,
            }}
          >
            {lowerThirdText}
          </div>
        </AbsoluteFill>
      ) : null}

      {activeWord ? (
        <AbsoluteFill
          style={{ justifyContent: 'flex-end', alignItems: 'center', paddingBottom: 140 }}
        >
          <div
            style={{
              fontFamily: 'Montserrat, sans-serif',
              fontSize: highlighted ? 80 : 64,
              fontWeight: 900,
              color: highlighted ? '#00F5D4' : '#FFFFFF',
              textTransform: 'uppercase',
              textShadow: '0px 8px 28px rgba(0,0,0,0.95)',
              backgroundColor: 'rgba(10, 12, 18, 0.72)',
              padding: '14px 36px',
              borderRadius: 18,
              border: highlighted ? '3px solid #00F5D4' : 'none',
            }}
          >
            {activeWord}
          </div>
        </AbsoluteFill>
      ) : null}
    </AbsoluteFill>
  );
};
