import React, {
  FC,
  lazy,
  LazyExoticComponent,
  Suspense,
  useEffect,
  useState,
} from "react";

import { Box } from "@mui/material";

import { LoadingIndicatorWithBackdrop } from "../common/Loader";

import {
  Aliases,
  Configuration,
  ConfigurationContext,
  DefaultConfiguration,
} from "../context/configuration";

import {
  Authentication,
  AuthenticationContext,
} from "../context/authentication";

import {
  loadLanguageSettings,
  loadNetworkSettings,
  loadServerInformation,
  loadServerUrl,
} from "../data/browserCache";

import { useFadeIn } from "../styles/effects";

import "../styles/fading.css";

const Login = lazy(() => import("./Login"));
const ChatView = lazy(() => import("./Viewport"));
const Banner = lazy(() => import("./Banner"));

export const App: FC = () => {
  const [fadeIn, setFadeIn] = useFadeIn();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [OpenConfigDialog, setOpenConfigDialog] =
    React.useState<LazyExoticComponent<FC<any>>>();

  // Setup Configuration Context
  const [serverUrl, setServerUrl] = useState(loadServerUrl());
  const [networkSettings, setNetworkSettings] = useState(loadNetworkSettings());
  const [languageSettings, setLanguageSettings] = useState(
    loadLanguageSettings(),
  );
  const [aliases, setAliases] = useState<Aliases>(DefaultConfiguration.aliases);

  // Setup Authentication Context
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [authServer, setAuthServer] = useState(loadServerInformation());

  const authContext: Authentication = {
    isAuthenticated,
    setIsAuthenticated,
    authServer,
    setAuthServer,
  };

  const configContext: Configuration = {
    serverUrl,
    setServerUrl,
    networkSettings,
    setNetworkSettings,
    languageSettings,
    setLanguageSettings,
    aliases,
    setAliases,
  };

  useEffect(() => {
    setFadeIn(true);
    // Test localStorage for configuration settings
    const serverUrl = loadServerUrl();
    const networkSettings = loadNetworkSettings();
    const languageSettings = loadLanguageSettings();
    const authServer = loadServerInformation();
    // Set the configuration context if the settings are defined.
    if (serverUrl !== undefined) setServerUrl(serverUrl);
    if (networkSettings !== undefined) setNetworkSettings(networkSettings);
    if (languageSettings !== undefined) setLanguageSettings(languageSettings);
    if (authServer !== undefined) setAuthServer(authServer);
  }, []);

  return (
    <ConfigurationContext.Provider value={configContext}>
      <AuthenticationContext.Provider value={authContext}>
        <Box className={fadeIn ? "fade-in" : ""}>
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
              openServerUrlDialog={() => setLoginOpen(true)}
            />
            <Login isOpen={loginOpen} onClose={() => setLoginOpen(false)} />
            <Suspense fallback={<LoadingIndicatorWithBackdrop />}>
              {OpenConfigDialog && (
                <OpenConfigDialog
                  isOpen={dialogOpen}
                  onClose={() => setDialogOpen(false)}
                />
              )}
            </Suspense>
            <ChatView />
          </Box>
        </Box>
      </AuthenticationContext.Provider>
    </ConfigurationContext.Provider>
  );
};

export default App;
