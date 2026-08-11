import { useEffect, useRef, useState } from 'react';

interface PanelOptions {
  storageKey: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
}

interface DividerProps {
  width: number;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  minRemainingWidth: number;
  label: string;
  onResize: (width: number) => void;
}

const DIVIDER_WIDTH = 8;

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.min(Math.max(value, minimum), maximum);

/** 读取并持久化单个分栏宽度，不把纯 UI 偏好放入业务 Store。 */
export function useResizablePanel({
  storageKey,
  defaultWidth,
  minWidth,
  maxWidth,
}: PanelOptions) {
  const [width, setWidth] = useState(() => {
    const stored = Number.parseFloat(window.localStorage.getItem(storageKey) ?? '');
    return Number.isFinite(stored) ? clamp(stored, minWidth, maxWidth) : defaultWidth;
  });

  useEffect(() => {
    window.localStorage.setItem(storageKey, String(width));
  }, [storageKey, width]);

  return {
    width,
    setWidth,
  };
}

/** 鼠标、触控笔和键盘均可操作的竖向区域分隔条。 */
export default function ResizableDivider({
  width,
  defaultWidth,
  minWidth,
  maxWidth,
  minRemainingWidth,
  label,
  onResize,
}: DividerProps) {
  const dividerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const availableMaximum = () => {
    const parentWidth = dividerRef.current?.parentElement?.getBoundingClientRect().width;
    if (!parentWidth) return maxWidth;
    return Math.max(minWidth, Math.min(maxWidth, parentWidth - minRemainingWidth - DIVIDER_WIDTH));
  };

  const resizeTo = (nextWidth: number) => {
    onResize(clamp(nextWidth, minWidth, availableMaximum()));
  };

  const finishDrag = () => {
    dragRef.current = null;
    setDragging(false);
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  };

  useEffect(() => {
    const parent = dividerRef.current?.parentElement;
    if (!parent) return;
    const observer = new ResizeObserver(() => resizeTo(width));
    observer.observe(parent);
    return () => observer.disconnect();
  }, [maxWidth, minRemainingWidth, minWidth, onResize, width]);

  useEffect(
    () => () => {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    },
    [],
  );

  return (
    <div
      ref={dividerRef}
      className="resize-divider"
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={minWidth}
      aria-valuemax={maxWidth}
      aria-valuenow={Math.round(width)}
      data-dragging={dragging}
      tabIndex={0}
      title="拖动调整宽度，双击恢复默认宽度"
      onDoubleClick={() => resizeTo(defaultWidth)}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        dragRef.current = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startWidth: width,
        };
        setDragging(true);
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current;
        if (!drag || drag.pointerId !== event.pointerId) return;
        resizeTo(drag.startWidth + event.clientX - drag.startX);
      }}
      onPointerUp={(event) => {
        if (dragRef.current?.pointerId !== event.pointerId) return;
        event.currentTarget.releasePointerCapture(event.pointerId);
        finishDrag();
      }}
      onPointerCancel={finishDrag}
      onLostPointerCapture={finishDrag}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft') {
          event.preventDefault();
          resizeTo(width - 10);
        } else if (event.key === 'ArrowRight') {
          event.preventDefault();
          resizeTo(width + 10);
        } else if (event.key === 'Home') {
          event.preventDefault();
          resizeTo(minWidth);
        } else if (event.key === 'End') {
          event.preventDefault();
          resizeTo(maxWidth);
        }
      }}
    />
  );
}
