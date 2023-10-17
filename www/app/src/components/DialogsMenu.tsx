import React, { FC, lazy, LazyExoticComponent } from "react";

import { Box, IconButton, Menu, MenuItem, useTheme } from "@mui/material";

import { Settings as SettingsIcon } from "@mui/icons-material";
import { useAuthentication } from "../context/authentication";

export interface ConfigDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

const ConfigDialogs: {
  [label: string]: LazyExoticComponent<FC<ConfigDialogProps>>;
} = {
  Settings: lazy(() => import("./config/SettingsDialog")),
  Aliases: lazy(() => import("./config/AliasDialog")),
};

export interface SettingsMenuProps {
  setOpenConfigDialog: (dialog: LazyExoticComponent<FC<any>>) => void;
}

const DialogsMenu: FC<SettingsMenuProps> = ({ setOpenConfigDialog }) => {
  const theme = useTheme();
  const { isAuthenticated } = useAuthentication();
  const [anchorEl, setAnchorEl] = React.useState<null | HTMLElement>(null);
  const isOpen = Boolean(anchorEl);

  const handleClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    setAnchorEl(event.currentTarget);
  };

  const handleClose = () => {
    setAnchorEl(null);
  };

  return (
    <Box>
      <IconButton
        size="medium"
        aria-label="Settings"
        onClick={handleClick}
        disabled={!isAuthenticated}
      >
        <SettingsIcon />
      </IconButton>
      <Menu anchorEl={anchorEl} open={isOpen} onClose={handleClose}>
        {Object.keys(ConfigDialogs).map((label) => {
          return (
            <MenuItem
              key={label}
              onClick={() => {
                setOpenConfigDialog(ConfigDialogs[label]);
                handleClose();
              }}
            >
              {label}
            </MenuItem>
          );
        })}
      </Menu>
    </Box>
  );
};

export default DialogsMenu;
