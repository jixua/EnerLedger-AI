import { useEffect } from "react";

let lockCount = 0;

/**
 * 弹窗打开期间锁住正文滚动。
 *
 * 应用的滚动容器是 AppShell 里的 .page-viewport（body 本身不滚动），而弹窗遮罩不算
 * 滚动容器，滚轮落在遮罩上会穿透到它后面把正文滚走，所以这里显式锁住。
 */
export function useScrollLock(active) {
  useEffect(() => {
    if (!active) return undefined;
    const viewport = document.querySelector(".page-viewport");
    lockCount += 1;
    document.body.classList.add("scroll-locked");
    viewport?.classList.add("scroll-locked");
    return () => {
      lockCount -= 1;
      if (lockCount > 0) return;
      document.body.classList.remove("scroll-locked");
      viewport?.classList.remove("scroll-locked");
    };
  }, [active]);
}

export default useScrollLock;
