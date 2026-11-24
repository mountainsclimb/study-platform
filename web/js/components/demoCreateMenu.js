/* 笔记 / 课堂共用的「新建演示」菜单。
 *
 * 复用题目页已用的演示弹层（geometryDemo / timelineDemo / moleculeViewer /
 * physicsSandbox），创建时把附件关联到当前目标（note / classroom），保存成功后
 * 由调用方刷新列表。弹层仍负责参数校验与保存，本组件只做入口与错误提示。
 */
import { el, button, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';

export const DEMO_CREATORS = Object.freeze([
  {
    id: 'geometry_demo',
    label: () => t('study.oss.geometry.open'),
    load: () => import('./geometryDemo.js'),
    open: (module, options) => module.openGeometryDemo(options),
  },
  {
    id: 'timeline',
    label: () => t('study.oss.timeline.open'),
    load: () => import('./timelineDemo.js'),
    open: (module, options) => module.openTimelineDemo(options),
  },
  {
    id: 'molecule_3d',
    label: () => t('study.oss.molecule.open'),
    load: () => import('./moleculeViewer.js'),
    open: (module, options) => module.openMoleculeViewer(options),
  },
  {
    id: 'physics_sandbox',
    label: () => t('study.oss.sandbox.open'),
    load: () => import('./physicsSandbox.js'),
    open: (module, options) => module.openPhysicsSandbox(options),
  },
]);

/**
 * @param {{targetKind:'note'|'classroom', targetKey:string|number, onSaved?:Function,
 *          label?:string, savedText?:string, failedText?:string}} options
 * @returns {HTMLElement}
 */
export function createDemoCreateMenu({
  targetKind, targetKey, onSaved = null,
  label = '', savedText = '', failedText = '',
} = {}) {
  const details = el('details', { class: 'demo-create-menu' });
  const menu = el('div', { class: 'demo-create-list' });
  for (const creator of DEMO_CREATORS) {
    menu.append(button(creator.label(), async (event) => {
      event.preventDefault();
      details.open = false;
      try {
        const module = await creator.load();
        creator.open(module, {
          links: [{ target_kind: targetKind, target_key: String(targetKey) }],
          onSaved: (result) => {
            toast(savedText || '演示已保存');
            if (typeof onSaved === 'function') onSaved(result);
          },
        });
      } catch (error) {
        toast(error?.message || failedText || '演示打开失败', true);
      }
    }, 'ghost small'));
  }
  details.append(
    el('summary', { class: 'button soft small' }, label || '新建演示'),
    menu,
  );
  return details;
}
