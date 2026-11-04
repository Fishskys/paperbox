import type { Page } from "@playwright/test";

// Computed styles cover inherited colors and translucent Ant Design surfaces.
// This audit covers solid UI surfaces; PDF/image pixels need visual inspection.
export async function contrastFailures(page: Page) {
  return page.evaluate(() => {
    type Color = [number, number, number, number];
    const parse = (color: string): Color => {
      const values = color.match(/[\d.]+/g)?.map(Number) || [];
      return [values[0] || 0, values[1] || 0, values[2] || 0, values[3] ?? 1];
    };
    const over = (front: Color, back: Color): Color => [
      ...front.slice(0, 3).map((value, i) => value * front[3] + back[i] * (1 - front[3])), 1,
    ] as Color;
    const background = (element: Element | null): Color => {
      if (!element) return [255, 255, 255, 1];
      return over(parse(getComputedStyle(element).backgroundColor), background(element.parentElement));
    };
    const luminance = (color: Color) => color.slice(0, 3).map(value => {
      const channel = value / 255;
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
    const failures: string[] = [];
    function check(element: Element, text: string, placeholder = false, kind: "text" | "icon" | "border" = "text") {
      if (!text.trim() || element.closest("[disabled], [aria-disabled=true], .react-pdf__Page")) return;
      const style = getComputedStyle(element, placeholder ? "::placeholder" : undefined);
      if (kind === "border" && (style.borderTopStyle === "none" || parseFloat(style.borderTopWidth) === 0)) return;
      if (!element.getClientRects().length || style.visibility !== "visible" || style.display === "none") return;
      for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) {
        if (Number(getComputedStyle(ancestor).opacity) === 0) return;
      }
      const bg = background(element);
      const color = kind === "border" ? style.borderTopColor : style.color;
      const fg = over(parse(color), bg);
      const ratio = (Math.max(luminance(fg), luminance(bg)) + 0.05) / (Math.min(luminance(fg), luminance(bg)) + 0.05);
      const size = parseFloat(style.fontSize);
      const large = size >= 24 || (size >= 18.67 && Number(style.fontWeight) >= 700);
      const minimum = large || kind !== "text" ? 3 : 4.5;
      if (ratio + 0.005 < minimum) failures.push(`${text.trim().slice(0, 55)} [${element.getAttribute("class")}] ${ratio.toFixed(2)} < ${minimum} (${color})`);
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const element = walker.currentNode.parentElement;
      if (element && !element.closest("script, style, svg")) check(element, walker.currentNode.textContent || "");
    }
    for (const element of document.querySelectorAll<HTMLInputElement>("input, textarea")) {
      check(element, element.value || element.placeholder, !element.value);
    }
    for (const element of document.querySelectorAll("button svg")) {
      check(element, element.closest("button")?.getAttribute("aria-label") || "操作图标", false, "icon");
    }
    for (const element of document.querySelectorAll(".ant-input-outlined, .ant-input-affix-wrapper-outlined, .ant-input-number-outlined, .ant-select-outlined")) {
      check(element, "输入控件边界", false, "border");
    }
    return [...new Set(failures)];
  });
}
