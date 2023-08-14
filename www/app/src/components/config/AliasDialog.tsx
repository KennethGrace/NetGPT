import React, { FC } from "react";

import {
  Dialog,
  DialogTitle,
  DialogContent,
  TextField,
  Stack,
} from "@mui/material";

export interface ConfigDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

export const AliasDialog: FC<ConfigDialogProps> = ({ isOpen, onClose }) => {
  const [showPassword, setShowPassword] = React.useState(false);

  const handleClickShowPassword = () => {
    setShowPassword(!showPassword);
  };

  const handleMouseDownPassword = (
    event: React.MouseEvent<HTMLButtonElement>
  ) => {
    event.preventDefault();
  };
  return (
    <Dialog
      open={isOpen}
      aria-labelledby="settings-dialog-title"
      aria-describedby="settings-dialog-description"
      fullWidth
      maxWidth="xs"
    >
      <DialogTitle id="settings-dialog-title">Settings</DialogTitle>
      <DialogContent>
        <Stack direction={"column"} spacing={1}>
          <TextField
            autoFocus
            margin="dense"
            id="username"
            label="Username"
            type="text"
            fullWidth
            variant="outlined"
          />
        </Stack>
      </DialogContent>
    </Dialog>
  );
};

export default AliasDialog;
