import { PaletteMode, createTheme } from "@mui/material";

declare module "@mui/material/styles" {
  interface Palette {
    tertiary: Palette["secondary"];
    code: Palette["primary"];
  }

  interface PaletteOptions {
    tertiary?: PaletteOptions["secondary"];
    code?: PaletteOptions["primary"];
  }
}

export const getDesign = (mode: PaletteMode) => ({
  palette: {
    mode,
    ...(mode === "light"
      ? {
          // palette values for light mode
          primary: {
            main: "#FF9B00",
          },
          secondary: {
            main: "#77B8F9",
          },
          tertiary: {
            main: "#C6CDE4",
          },
          code: {
            main: "#efefef",
          },
        }
      : {
          // palette values for dark mode
          primary: {
            main: "#ce6b00",
          },
          secondary: {
            main: "#4764A9",
          },
          tertiary: {
            main: "#092A57",
          },
          code: {
            main: "#212121",
          },
        }),
  },
});

export const NetGPTTheme = (prefersDarkMode: boolean) =>
  createTheme(getDesign(prefersDarkMode ? "dark" : "light"));
