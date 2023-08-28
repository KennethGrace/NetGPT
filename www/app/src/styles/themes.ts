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
            main: "#8bd771",
          },
          secondary: {
            main: "#6fb2f5",
          },
          tertiary: {
            main: "#F9B571",
          },
          code: {
            main: "#efefef",
          },
        }
      : {
          // palette values for dark mode
          primary: {
            main: "#145200",
          },
          secondary: {
            main: "#0a2a56",
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
