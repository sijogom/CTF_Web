export type ColorVisionMode = "normal" | "protanopia" | "deuteranopia" | "tritanopia" | "high-contrast";
export type FontSizeLevel = "small" | "default" | "large";
export type ThemeMode = "dark" | "light" | "system";

export interface AccessibilityState {
  fontSize: FontSizeLevel;
  colorVisionMode: ColorVisionMode;
  themeMode: ThemeMode;
  keyboardNav: boolean;
  screenReader: boolean;
  reducedMotion: boolean;
  easyRead: boolean;
}

export const DEFAULT_A11Y: AccessibilityState = {
  fontSize: "default",
  colorVisionMode: "normal",
  themeMode: "light",
  keyboardNav: false,
  screenReader: false,
  reducedMotion: false,
  easyRead: false,
};
