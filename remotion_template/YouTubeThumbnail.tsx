import React from 'react';
import { AbsoluteFill, Img, staticFile } from 'remotion';

export interface ThumbnailProps {
  /** Teks hook besar, idealnya maksimal 4 kata. */
  hookText: string;
  /** Label pendek di atas judul (mis. "RAHASIA AI"). */
  subBadgeText?: string;
  /** Nama file gambar di public/kra_temp/ (boleh kosong → latar gradien). */
  bgImageSrc: string;
  /** Warna aksen: #FFE600 (kuning), #00F5D4 (cyan), #FF2A54 (merah). */
  accentColor?: string;
}

export const defaultThumbnailProps: ThumbnailProps = {
  hookText: 'MEREKA BEKERJA SAAT KITA TIDUR',
  subBadgeText: 'KANTOR AI OTONOM',
  bgImageSrc: '',
  accentColor: '#FFE600',
};

/**
 * Thumbnail YouTube 1280x720 untuk `npx remotion still YouTubeThumbnail out.jpg`.
 *
 * Aturan yang membuatnya lolos QC Damon:
 *  - kontras tinggi: teks putih + gradient gelap 95% di sisi kiri;
 *  - hook besar (>= 96 px) supaya tetap terbaca di layar HP;
 *  - bingkai aksen tipis agar thumbnail "berbunyi" di feed.
 *
 * Gambar latar dihasilkan `studio/thumbnail.py` (DALL·E/Flux/kanvas FFmpeg) dan
 * disalin ke `remotion_studio/public/kra_temp/`, lalu diakses lewat `staticFile()`.
 */
export const YouTubeThumbnail: React.FC<ThumbnailProps> = ({
  hookText,
  subBadgeText = 'FAKTA MENGEJUTKAN',
  bgImageSrc,
  accentColor = '#FFE600',
}) => {
  return (
    <AbsoluteFill style={{ backgroundColor: '#090A0F', overflow: 'hidden' }}>
      {bgImageSrc ? (
        <Img
          src={staticFile(`kra_temp/${bgImageSrc}`)}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            transform: 'scale(1.04)',
            filter: 'contrast(1.15) saturate(1.2)',
          }}
        />
      ) : (
        <AbsoluteFill
          style={{ background: 'radial-gradient(circle at 70% 30%, #1b1440 0%, #090A0F 72%)' }}
        />
      )}

      {/* Gradient gelap di sisi teks agar tetap terbaca di HP */}
      <AbsoluteFill
        style={{
          background:
            'linear-gradient(90deg, rgba(5,7,12,0.95) 0%, rgba(5,7,12,0.78) 52%, rgba(5,7,12,0.15) 100%)',
        }}
      />

      <AbsoluteFill
        style={{
          justifyContent: 'center',
          alignItems: 'flex-start',
          paddingLeft: 68,
          paddingRight: 300,
        }}
      >
        {subBadgeText ? (
          <div
            style={{
              backgroundColor: accentColor,
              color: '#090A0F',
              fontFamily: 'Montserrat, Inter, sans-serif',
              fontSize: 32,
              fontWeight: 900,
              padding: '8px 24px',
              borderRadius: 10,
              textTransform: 'uppercase',
              letterSpacing: 1.5,
              marginBottom: 22,
              boxShadow: '0 8px 24px rgba(0,0,0,0.6)',
            }}
          >
            {subBadgeText}
          </div>
        ) : null}

        <div
          style={{
            fontFamily: 'Montserrat, Inter, sans-serif',
            fontSize: 108,
            fontWeight: 900,
            lineHeight: 1.02,
            letterSpacing: -1.5,
            color: '#FFFFFF',
            textTransform: 'uppercase',
            textShadow: '0px 10px 30px rgba(0,0,0,0.95)',
            WebkitTextStroke: '2px rgba(0,0,0,0.5)',
          }}
        >
          {hookText}
        </div>
      </AbsoluteFill>

      <AbsoluteFill style={{ border: `10px solid ${accentColor}`, pointerEvents: 'none' }} />
    </AbsoluteFill>
  );
};
