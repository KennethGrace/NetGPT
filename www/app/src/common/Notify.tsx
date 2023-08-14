import React, {FC} from "react";

import { 
    Snackbar,
    Alert,
    AlertTitle
} from "@mui/material";

import {
    Alarm,
} from "@mui/icons-material";

const severityTitles = {
    success: "Success",
    info: "Info",
    warning: "Warning",
    error: "Error"
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
    severity
}) => {
    const icon = {
        success: <Alarm />,
        info: <Alarm />,
        warning: <Alarm />,
        error: <Alarm />
    }[severity];

    return (
        <Snackbar
            open={isOpen}
            autoHideDuration={6000}
            onClose={onClose}
            anchorOrigin={{ vertical: "top", horizontal: "center" }}
        >
            <Alert icon={icon} severity={severity} onClose={onClose}>
                <AlertTitle>{severityTitles[severity]}</AlertTitle>
                {message}
            </Alert>
        </Snackbar>
    );
}