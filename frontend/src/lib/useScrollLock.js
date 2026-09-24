import { useEffect } from "react";

let lockCount = 0;

/**
 * 弹窗打开期间锁住正文滚动。
 *
 * 应用的滚动容器是工作台里那一块正在显示的面板（`.page-pane.is-active`；早期只有
 * 一个 `.page-viewport`），body 本身不滚动。弹窗遮罩不算滚动容器，滚轮落在遮罩上会
 * 穿透到它后面把正文滚走，所以这里显式锁住。
 *
 * 必须按「当前显示的那一块」来找：面板是常驻的，只查第一个会把滚动锁到某个
 * 已经被隐藏的面板上，眼前这一屏照样滚。
 */
export function useScrollLock(active) {
  useEffect(() => {
    if (!active) return undefined;
    const viewport = document.querySelector(".page-pane.is-active") || document.querySelector(".page-viewport");
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
