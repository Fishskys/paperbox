import { describe, expect, it } from "vitest";
import { findText, highlightText } from "../src/pdf-text";

describe("PDF text", () => {
  it("finds literal case-insensitive phrases across whitespace", () => {
    expect(findText("Clock \n delay, clock delay", "clock delay")?.count).toBe(2);
    expect(findText("abc", "[a]*")).toBeNull();
    expect(findText("abc", " ")).toBeNull();
  });
  it("escapes PDF text and literal queries before highlighting", () => {
    expect(highlightText('<img src=x onerror="bad"> & [x]', "[x]")).toBe('&lt;img src=x onerror=&quot;bad&quot;&gt; &amp; <mark>[x]</mark>');
    expect(highlightText("Clock clock", "CLOCK")).toBe("<mark>Clock</mark> <mark>clock</mark>");
    expect(highlightText("<script>", "")).toBe("&lt;script&gt;");
  });
});