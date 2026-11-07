import React from 'react';
import { Composition, Still } from 'remotion';
import { StoryScene, StorySceneProps, defaultStoryProps } from './StoryScene';
import { YouTubeThumbnail, defaultThumbnailProps } from './YouTubeThumbnail';

/**
 * Tiga composition yang dipakai studio AutoGen (Raka):
 *
 *  - `StoryScene`       → scene narasi (teks per kata + video sumber)
 *  - `CreatorOverlay`   → scene A-Roll Creator + lower third
 *  - `YouTubeThumbnail` → Still 1280x720 untuk `npx remotion still`
 *
 * Semua composition/scene memakai props dari `script_production.json`
 * (lewat `--props` saat `render`) atau `thumb_props_*.json` (saat `still`).
 */

const FPS = 30;

/** Duration dihitung dari Whisper (durationInSeconds), bukan angka statis. */
const calculateMetadata = async ({ props }: { props: StorySceneProps }) => {
  const seconds = Math.max(1, Number(props?.durationInSeconds) || 5);
  return {
    durationInFrames: Math.max(1, Math.ceil(seconds * FPS)),
    props,
  };
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="StoryScene"
        component={StoryScene}
        fps={FPS}
        width={1920}
        height={1080}
        defaultProps={defaultStoryProps}
        calculateMetadata={calculateMetadata}
      />
      <Composition
        id="CreatorOverlay"
        component={StoryScene}
        fps={FPS}
        width={1920}
        height={1080}
        defaultProps={{ ...defaultStoryProps, showLowerThird: true }}
        calculateMetadata={calculateMetadata}
      />
      <Still
        id="YouTubeThumbnail"
        component={YouTubeThumbnail}
        width={1280}
        height={720}
        defaultProps={defaultThumbnailProps}
      />
    </>
  );
};
