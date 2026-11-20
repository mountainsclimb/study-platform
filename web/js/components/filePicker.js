import { el, icon } from '../core/dom.js';

export function filePicker(label, help, accept, onFiles, { multiple = false, capture = null } = {}) {
  const attrs = { type: 'file', accept, 'aria-label': label, multiple };
  if (capture) attrs.capture = capture;
  const fileInput = el('input', attrs);
  const node = el('label', { class: 'upload-zone' },
    icon('upload'),
    el('span', {}, label),
    el('small', {}, help),
    fileInput);

  fileInput.addEventListener('change', () => {
    const list = [...(fileInput.files || [])];
    if (list.length) onFiles(multiple ? list : list[0]);
    fileInput.value = '';
  });

  for (const eventName of ['dragenter', 'dragover']) {
    node.addEventListener(eventName, event => {
      event.preventDefault();
      node.classList.add('drop-enter');
    });
  }
  for (const eventName of ['dragleave', 'drop']) {
    node.addEventListener(eventName, event => {
      event.preventDefault();
      node.classList.remove('drop-enter');
      if (eventName === 'drop' && event.dataTransfer?.files?.length) {
        const list = [...event.dataTransfer.files];
        onFiles(multiple ? list : list[0]);
      }
    });
  }
  return node;
}

export function attachPasteImage(container, onImageFile) {
  const handler = event => {
    if (!container.isConnected) {
      window.removeEventListener('paste', handler);
      return;
    }
    const items = event.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) {
          event.preventDefault();
          onImageFile(new File([file], `截图_${Date.now()}.png`, { type: file.type || 'image/png' }));
          break;
        }
      }
    }
  };
  window.addEventListener('paste', handler);
  return () => window.removeEventListener('paste', handler);
}
