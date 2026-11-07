import React from 'react';
import { Composition } from 'remotion';
import { StoryScene, StorySceneProps, defaultStoryProps } from './StoryScene';

/**
 * Dua composition yang dipakai studio AutoGen (Raka):
 *
 *  - `StoryScene`       → scene narasi (teks per kata + video sumber)
 *  - `CreatorOverlay`   → scene A-Roll Creator + lower third
 *
 * Keduanya memakai `StoryScene`, jadi props dari `script_production.json`
 * (lewat `--props` saat `npx remotion render`) selalu cocok.
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
    </>
  );
};
