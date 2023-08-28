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

const DialogsMenu = lazy(() => import("./DialogsMenu"));

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

  const urlButtonColor = useMemo(() => {
    if (serverUrl) {
      return isAuthenticated ? "primary" : "warning";
    }
    return "inherit";
  }, [serverUrl, isAuthenticated]);

  const ServerURLButton = useMemo(() => {
    if (serverUrl) {
      return () => (
        <Tooltip title={"Edit the Server URL"} arrow>
          <Button
            variant="contained"
            endIcon={<Api />}
            onClick={() => {
              openServerUrlDialog();
            }}
            aria-label="set-server-url"
            color={urlButtonColor}
          >
            {serverUrl}
          </Button>
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
  }, [serverUrl, urlButtonColor]);

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
