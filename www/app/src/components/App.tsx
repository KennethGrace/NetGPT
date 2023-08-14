import React, {
  FC,
  lazy,
  LazyExoticComponent,
  Suspense,
  useEffect,
  useState,
} from "react";

import { AppBar, Box, IconButton, Toolbar, Typography } from "@mui/material";

import { GitHub } from "@mui/icons-material";

import { LoadingIndicatorWithBackdrop } from "../common/Loader";

import {
  ConfigurationContext,
  NetworkSettings,
  Aliases,
  LanguageSettings,
  DefaultConfiguration,
} from "../common/configuration";

import {
  loadServerUrl,
  loadNetworkSettings,
  loadLanguageSettings,
} from "../data/browserCache";

import { useFadeIn } from "../styles/effects";

import "../styles/effect.css";

const ChatView = lazy(() => import("./ChatView"));
const DialogsMenu = lazy(() => {
  return new Promise((resolve) => setTimeout(resolve, 2000)).then(
    () => import("./DialogsMenu")
  );
});

const ApplicationTitle: string = "NetGPT";
const ApplicationGithub: string = "https://github.com/kennethgrace/netgpt";

const MidScreenLogo = () => {
  return <Box
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
}

interface BannerProps {
  setOpenConfigDialog: (dialog: LazyExoticComponent<FC<any>>) => void;
}

const Banner: FC<BannerProps> = ({ setOpenConfigDialog }) => {
  const handleGitHub = () => {
    window.open(ApplicationGithub, "_blank");
  };

  return (
    <AppBar
      position="sticky"
      sx={{
        backgroundColor: "primary.main",
        flexDirection: "row",
        padding: "8px",
        height: "64px",
        alignItems: "center",
      }}
    >
      <Toolbar
        sx={{
          justifyContent: "flex-start",
        }}
      >
        <DialogsMenu setOpenConfigDialog={setOpenConfigDialog} />
      </Toolbar>
      <Toolbar
        sx={{
          flexGrow: 1,
          justifyContent: "center",
        }}
      >
        <Typography variant="h4" fontWeight="bold" noWrap>
          {ApplicationTitle}
        </Typography>
      </Toolbar>
      <Toolbar>
        <IconButton size="medium" aria-label="GitHub" onClick={handleGitHub}>
          <GitHub />
        </IconButton>
      </Toolbar>
    </AppBar>
  );
};

export const App: FC = () => {
  const fadeIn = useFadeIn();
  const [dialogOpen, setDialogOpen] = React.useState(false);
  const [OpenConfigDialog, setOpenConfigDialog] =
    React.useState<LazyExoticComponent<FC<any>>>();

  // Setup Configuration Context
  const [serverUrl, setServerUrl] = useState<string>(
    DefaultConfiguration.serverUrl
  );
  const [networkSettings, setNetworkSettings] = useState<NetworkSettings>();
  const [languageSettings, setLanguageSettings] = useState<LanguageSettings>();
  const [aliases, setAliases] = useState<Aliases>(DefaultConfiguration.aliases);

  const configContext = {
    serverUrl,
    setServerUrl,
    networkSettings,
    setNetworkSettings,
    languageSettings,
    setLanguageSettings,
    aliases,
    setAliases,
  };

  // Check the browser's local storage for a saved configuration
  useEffect(() => {
    const serverUrl = loadServerUrl();
    const networkSettings = loadNetworkSettings();
    const languageSettings = loadLanguageSettings();

    setServerUrl(serverUrl ?? DefaultConfiguration.serverUrl);
    setNetworkSettings(networkSettings);
    setLanguageSettings(languageSettings);
  }, []);

  return (
    <ConfigurationContext.Provider value={configContext}>
      <Box className={fadeIn ? "fade-in" : ""}>
        <MidScreenLogo />
        <Box
          component="main"
          sx={{
            display: "flex",
            flexDirection: "column",
            width: "100vw",
            height: "100vh",
          }}
        >
          <Banner
            setOpenConfigDialog={(dialog) => {
              setOpenConfigDialog(dialog);
              setDialogOpen(true);
            }}
          />
          <Suspense fallback={<LoadingIndicatorWithBackdrop />}>
            {OpenConfigDialog && (
              <OpenConfigDialog isOpen={dialogOpen} onClose={setDialogOpen} />
            )}
          </Suspense>
          <ChatView />
        </Box>
      </Box>
    </ConfigurationContext.Provider>
  );
};

export default App;
