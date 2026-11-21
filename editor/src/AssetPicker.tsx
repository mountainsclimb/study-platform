/**
 * 图片地址选择器：替换上游 `DefaultAssetPicker`（它会把本地文件转成 data URL，
 * 知序后端只持久化站内相对地址，data URL / 外链会在保存时被丢弃）。
 *
 * 因此这里只接受同源相对地址（如 `/api/classroom/assets/12`），并在应用前
 * 用 `Image` 预加载确认可显示。离线课堂不引入任何外网资源。
 */
import React, { useEffect, useRef, useState } from 'react';
import type { EditorAssetPickerRequest } from '@openmaic/editor/ui';
import { normalizeImageSrc } from './types';

export function AssetUrlPicker({ request }: { request: EditorAssetPickerRequest }) {
  const [value, setValue] = useState(request.currentSrc && request.currentSrc.startsWith('/') ? request.currentSrc : '');
  const [error, setError] = useState('');
  const [checking, setChecking] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const apply = () => {
    const src = normalizeImageSrc(value);
    if (!src) {
      setError('请输入站内相对地址（以 / 开头，如 /api/classroom/assets/12）；不支持外链或本地上传。');
      return;
    }
    setChecking(true);
    setError('');
    const image = new Image();
    image.onload = () => {
      if (!mountedRef.current) return;
      setChecking(false);
      request.onPick({ src, width: image.naturalWidth, height: image.naturalHeight });
      request.close();
    };
    image.onerror = () => {
      if (!mountedRef.current) return;
      setChecking(false);
      setError('该地址加载不到图片，请确认资源存在且为站内地址。');
    };
    image.src = src;
  };

  return (
    <div className="zx-asset-picker" role="dialog" aria-label="选择图片地址">
      <p className="zx-asset-picker-hint">
        离线课堂只支持站内图片地址（例如 <code>/api/classroom/assets/12</code>）。
        不支持外链 URL 与本地上传（会被后端丢弃）。
      </p>
      <div className="zx-asset-picker-row">
        <input
          type="text"
          value={value}
          placeholder="/api/classroom/assets/12"
          aria-label="图片站内地址"
          onChange={(event) => {
            setValue(event.target.value);
            setError('');
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') apply();
          }}
        />
        <button type="button" className="zx-btn zx-btn-primary" disabled={checking} onClick={apply}>
          {checking ? '校验中…' : '使用'}
        </button>
        <button type="button" className="zx-btn" onClick={request.close}>
          取消
        </button>
      </div>
      {error ? <p className="zx-asset-picker-error">{error}</p> : null}
    </div>
  );
}
