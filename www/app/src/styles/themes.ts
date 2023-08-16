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
            main: "#71B5F9",
          },
          tertiary: {
            main: "#F9B571",
          },
          code: {
            main: "#efefef",
          },
          background: {
            paper: "#fff",
            default: "#e0e0e0",
          },
        }
      : {
          // palette values for dark mode
          primary: {
            main: "#CD6200",
          },
          secondary: {
            main: "#092A57",
          },
          tertiary: {
            main: "#573609",
          },
          code: {
            main: "#212121",
          },
        }),
  },
});

export const NetGPTTheme = (prefersDarkMode: boolean) =>
  createTheme(getDesign(prefersDarkMode ? "dark" : "light"));
