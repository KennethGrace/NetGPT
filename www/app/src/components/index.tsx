// React
import ReactDOM from "react-dom";
import React, { FC, Suspense, lazy, useMemo } from "react";

// MUI
import { ThemeProvider, Box, CssBaseline, useMediaQuery } from "@mui/material";
import { NetGPTTheme } from "../styles/themes";

// Loading Indicator
import { LoadingIndicator } from "../common/Loader";

const app = document.getElementById("app");
const Application = lazy(() => {
  // Wait for 3 seconds before loading the application
  // to give the user a chance to see the logo.
  return new Promise((resolve) => setTimeout(resolve, 2000)).then(
    () => import("./App")
  );
});

const Index: FC<any> = (props: any) => {
  const prefersDarkMode = useMediaQuery("(prefers-color-scheme: dark)");

  // Application Theme
  const theme = useMemo(() => NetGPTTheme(prefersDarkMode), [prefersDarkMode]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box
        sx={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          opacity: 0.4,
        }}
      >
        <img alt="logo" aria-label="logo" src="/logo256x256.png" />
      </Box>
      <Suspense fallback={<LoadingIndicator label="Loading NetGPT" />}>
        <Application />
      </Suspense>
    </ThemeProvider>
  );
};

ReactDOM.render(
  <React.StrictMode>
    <CssBaseline />
    <Index />
  </React.StrictMode>,
  app
);
