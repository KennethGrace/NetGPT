import React, {
  FC,
  lazy,
  LazyExoticComponent,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  AppBar,
  Button,
  IconButton,
  Stack,
  Toolbar,
  Tooltip,
  useTheme,
} from "@mui/material";

import { Api, GitHub } from "@mui/icons-material";
import { useConfiguration } from "../context/configuration";
import { getManifest, ManifestFile } from "../data/appManifest";
import { useAuthentication } from "../context/authentication";
import Pulse from "../common/Pulse";

const DialogsMenu = lazy(() => import("./DialogsMenu"));

const truncateUrl = (url: string) => {
  // Remove the protocol
  const noProtocol = url.replace("https://", "");
  // Remove the port
  const noPort = noProtocol.replace(/:\d+/, "");
  // Return the first 20 characters
  return noPort.substring(0, 20);
};

interface BannerProps {
  openServerUrlDialog: () => void;
  setOpenConfigDialog: (dialog: LazyExoticComponent<FC<any>>) => void;
}

const Banner: FC<BannerProps> = ({
  openServerUrlDialog,
  setOpenConfigDialog,
}) => {
  const theme = useTheme();
  const [manifest, setManifest] = useState<ManifestFile>();
  const { serverUrl } = useConfiguration();
  const { isAuthenticated, authServer } = useAuthentication();

  const handleGitHub = () => {
    window.open(manifest?.github_url, "_blank");
  };

  useEffect(() => {
    getManifest().then((manifest) => setManifest(manifest));
  }, []);

  const ServerURLButton = useMemo(() => {
    if (serverUrl) {
      const urlButtonColor = isAuthenticated ? "primary" : "warning";
      return () => (
        <Tooltip title={"Edit the Server URL"} arrow>
          <div>
            <Pulse disabled={isAuthenticated}>
              <Button
                variant="contained"
                endIcon={<Api />}
                onClick={() => {
                  openServerUrlDialog();
                }}
                aria-label="set-server-url"
                color={urlButtonColor}
              >
                {isAuthenticated ? truncateUrl(serverUrl) : "Not Connected"}
              </Button>
            </Pulse>
          </div>
        </Tooltip>
      );
    }
    return () => (
      <Tooltip title={"Configure the Server URL"} arrow>
        <IconButton
          size="medium"
          aria-label="set-server-url"
          onClick={openServerUrlDialog}
        >
          <Api />
        </IconButton>
      </Tooltip>
    );
  }, [serverUrl, isAuthenticated]);

  return (
    <AppBar
      position="sticky"
      sx={{
        flexDirection: "row",
        padding: "8px",
        height: "56px",
        alignItems: "center",
      }}
      color={"inherit"}
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
          justifyContent: "center",
          flexGrow: 1,
        }}
      >
        <Stack direction="row" spacing={0} alignItems="center">
          <ServerURLButton />
        </Stack>
      </Toolbar>

      <Toolbar
        sx={{
          justifyContent: "flex-end",
        }}
      >
        <IconButton size="medium" aria-label="GitHub" onClick={handleGitHub}>
          <GitHub />
        </IconButton>
      </Toolbar>
    </AppBar>
  );
};

export default Banner;
