import React, {
  FC,
  lazy,
  LazyExoticComponent,
  Suspense,
  useEffect,
  useMemo,
  useState,
} from "react";

import { Box } from "@mui/material";

import { LoadingIndicatorWithBackdrop } from "../common/Loader";

import {
  ConfigurationContext,
  NetworkSettings,
  Aliases,
  LanguageSettings,
  DefaultConfiguration,
  Configuration,
} from "../context/configuration";

import {
  AuthenticationServerInformation,
  Authentication,
  AuthenticationContext,
} from "../context/authentication";

import {
  loadServerUrl,
  loadNetworkSettings,
  loadLanguageSettings,
} from "../data/browserCache";

import { useFadeIn } from "../styles/effects";
import { getManifest, ManifestFile } from "../data/appManifest";

import "../styles/fading.css";

const Login = lazy(() => import("./Login"));
const ChatView = lazy(() => import("./Viewport"));
const Banner = lazy(() => import("./Banner"));

const MidScreenLogo = () => {
  return (
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
  );
};

export const App: FC = () => {
  const [fadeIn, setFadeIn] = useFadeIn();
  const [dialogOpen, setDialogOpen] = React.useState(false);
  const [OpenConfigDialog, setOpenConfigDialog] =
    React.useState<LazyExoticComponent<FC<any>>>();

  // Setup Configuration Context
  const [serverUrl, setServerUrl] = useState<string>(
    DefaultConfiguration.serverUrl,
  );
  const [networkSettings, setNetworkSettings] = useState<NetworkSettings>();
  const [languageSettings, setLanguageSettings] = useState<LanguageSettings>();
  const [aliases, setAliases] = useState<Aliases>(DefaultConfiguration.aliases);

  // Setup Authentication Context
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [authServer, setAuthServer] =
    useState<AuthenticationServerInformation>();

  const authContext: Authentication = {
    authServer,
    setAuthServer,
    isAuthenticated,
    setIsAuthenticated,
  };

  // Setup Application Manifest
  const [manifest, setManifest] = useState<ManifestFile>();

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
    getManifest().then((manifest) => {
      if (manifest) {
        setManifest(manifest);
      }
    });
    setFadeIn(true);
  }, []);

  // Check the browser's local storage for a saved configuration on startup
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
      <AuthenticationContext.Provider value={authContext}>
        {manifest ? (
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
                title={manifest.short_name}
                github_url={manifest.github_url}
                license_url={manifest.license_url}
                setOpenConfigDialog={(dialog) => {
                  setOpenConfigDialog(dialog);
                  setDialogOpen(true);
                }}
              />
              <Suspense fallback={<LoadingIndicatorWithBackdrop />}>
                <Login isOpen={!isAuthenticated} />
                {OpenConfigDialog && (
                  <OpenConfigDialog
                    isOpen={dialogOpen}
                    onClose={setDialogOpen}
                  />
                )}
              </Suspense>
              <ChatView />
            </Box>
          </Box>
        ) : (
          <LoadingIndicatorWithBackdrop label="Waiting for Application Manifest" />
        )}
      </AuthenticationContext.Provider>
    </ConfigurationContext.Provider>
  );
};

export default App;
