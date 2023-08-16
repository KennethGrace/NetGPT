import React, { FC, LazyExoticComponent, lazy, useMemo } from "react";

import {
  AppBar,
  Divider,
  IconButton,
  Paper,
  Stack,
  Toolbar,
  Typography,
  useTheme,
} from "@mui/material";

import { Copyright, GitHub } from "@mui/icons-material";
import { useConfiguration } from "../common/configuration";

const DialogsMenu = lazy(async () => {
  await new Promise((resolve) => setTimeout(resolve, 2000));
  return await import("./DialogsMenu");
});

interface BannerProps {
  title: string;
  github_url: string;
  license_url: string;
  setOpenConfigDialog: (dialog: LazyExoticComponent<FC<any>>) => void;
}

const Banner: FC<BannerProps> = ({
  title,
  github_url,
  license_url,
  setOpenConfigDialog,
}) => {
  const theme = useTheme();
  const handleGitHub = () => {
    window.open(github_url, "_blank");
  };

  return (
    <AppBar
      position="sticky"
      sx={{
        backgroundColor: "primary.main",
        flexDirection: "row",
        padding: "8px",
        height: "48px",
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
          justifyContent: "center",
          flexGrow: 1,
        }}
      >
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="h5" fontWeight="bold" noWrap>
            {title}
          </Typography>
        </Stack>
      </Toolbar>

      <Toolbar
        sx={{
          justifyContent: "flex-end",
        }}
      >
        <IconButton size="medium" aria-label="GitHub" onClick={handleGitHub}>
          <GitHub htmlColor={theme.palette.primary.contrastText} />
        </IconButton>
      </Toolbar>
    </AppBar>
  );
};

export default Banner;
