import React, { FC, useEffect } from "react";

import { Snackbar, Alert, AlertTitle } from "@mui/material";
import {
  Info,
  Warning,
  Error as ErrorIcon,
  CheckCircle,
} from "@mui/icons-material";

const severityTitles = {
  success: "Success",
  info: "Info",
  warning: "Warning",
  error: "Error",
};

export interface NotifyProps {
  isOpen: boolean;
  onClose: () => void;
  message: string;
  severity: "success" | "info" | "warning" | "error";
}

export const Notification: FC<NotifyProps> = ({
  isOpen,
  onClose,
  message,
  severity,
}) => {
  const icon = {
    success: <CheckCircle />,
    info: <Info />,
    warning: <Warning />,
    error: <ErrorIcon />,
  }[severity];

  useEffect(() => {
    if (isOpen) {
      console.log(`Notification: ${message}`);
    }
  }, [isOpen, message]);

  return (
    <Snackbar
      open={isOpen}
      autoHideDuration={6000}
      anchorOrigin={{ vertical: "top", horizontal: "center" }}
      onClose={onClose}
    >
      <Alert icon={icon} severity={severity}>
        <AlertTitle>{severityTitles[severity]}</AlertTitle>
        {message}
      </Alert>
    </Snackbar>
  );
};
