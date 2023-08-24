import React, { FC, useEffect, useMemo, useState, lazy, Suspense } from "react";

import {
  Box,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  IconButton,
  Button,
  TextField,
  Stack,
  Slide,
  LinearProgress,
} from "@mui/material";

import { TransitionProps } from "@mui/material/transitions";

import { Cancel, Save, Refresh, Lan } from "@mui/icons-material";

import {
  useConfiguration,
  LanguageSettings,
  NetworkSettings,
} from "../../context/configuration";

import { Notification } from "../../common/Notify";

import {
  DeviceOptions,
  LanguageSettingsList,
  fetchDeviceOptions,
  fetchLanguageSettingsList,
} from "../../server/configuring";

import {
  saveServerUrl,
  saveNetworkSettings,
  saveLanguageSettings,
} from "../../data/browserCache";

// Lazy Load the LanguageField component
const LanguageField = lazy(() => import("./LanguageField"));
const NetworkField = lazy(() => import("./NetworkField"));

const Transition = React.forwardRef(function Transition(
  props: TransitionProps & {
    children: React.ReactElement<any, any>;
  },
  ref: React.Ref<unknown>,
) {
  return (
    <Slide direction="up" ref={ref} {...props} children={props.children} />
  );
});

export interface SettingsDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

export const SettingsDialog: FC<SettingsDialogProps> = ({
  isOpen,
  onClose,
}) => {
  const { serverUrl, setNetworkSettings, setLanguageSettings } =
    useConfiguration();
  // State variables for UI
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [network, setNetwork] = useState<NetworkSettings>({
    username: "",
    password: "",
    deviceType: "",
  });
  const [language, setLanguage] = useState<LanguageSettings>({
    name: "",
    description: "",
    fields: {},
  });

  // State Variables for Server-Side Data
  const [languageOptions, setLanguageOptions] = useState<LanguageSettingsList>({
    settings: [],
  });
  const [deviceTypeOptions, setDeviceTypeOptions] = useState<DeviceOptions>({
    options: [],
  });

  // Use a memo to check if the form is valid before enabling the save button
  const isValid: boolean = useMemo(() => {
    // Check if the username and password are greater than 0 characters
    if (network.username.length === 0 || network.password.length === 0) {
      return false;
    }
    // Check if the device type is a valid option from the device type list
    if (!deviceTypeOptions.options.includes(network.deviceType)) {
      return false;
    }
    // Check if the language is a valid option from the language list
    if (!languageOptions.settings.find((l) => l.name === language.name)) {
      return false;
    }
    // Check that all the language fields are filled out
    if (
      Object.entries(language.fields).some(([_, value]) => !(value.length > 0))
    ) {
      console.log("Invalid Language Fields");
      console.log(language.fields);
      return false;
    }
    return true;
  }, [network, language]);

  const saveAndClose = () => {
    if (isValid) {
      saveLanguageSettings(language);
      setLanguageSettings(language);
      saveNetworkSettings(network);
      setNetworkSettings(network);
      onClose();
    }
  };

  const updateSettings = () => {
    if (serverUrl === undefined) {
      setErrorMessage("Please configure the server.");
      return;
    }
    fetchDeviceOptions(serverUrl)
      .then((options) => {
        setDeviceTypeOptions(options);
      })
      .catch((error) => {
        setErrorMessage(error.message);
      });
    fetchLanguageSettingsList(serverUrl)
      .then((options) => {
        setLanguageOptions(options);
      })
      .catch((error) => {
        setErrorMessage(error.message);
      });
  };

  useEffect(() => {
    if (isOpen) {
      updateSettings();
    }
  }, [isOpen]);

  return (
    <Dialog
      open={isOpen ?? false}
      TransitionComponent={Transition}
      aria-labelledby="settings-dialog-title"
      aria-describedby="settings-dialog-description"
      fullWidth
      maxWidth="xs"
    >
      <Notification
        isOpen={errorMessage !== null}
        onClose={() => setErrorMessage(null)}
        severity="error"
        message={errorMessage ?? "An unknown error occurred."}
      />
      <DialogTitle id="settings-dialog-title">Server Settings</DialogTitle>
      <DialogContent>
        <Stack direction={"column"} spacing={1}>
          <Suspense fallback={<LinearProgress color="primary" />}>
            <NetworkField
              title="Network Administration Settings"
              deviceOptions={deviceTypeOptions.options}
              setSelection={setNetwork}
            />
            <LanguageField
              title="Natural Language Settings"
              languages={languageOptions.settings}
              setSelection={setLanguage}
            />
          </Suspense>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button
          variant="contained"
          onClick={() => onClose()}
          color="inherit"
          endIcon={<Cancel />}
        >
          Cancel
        </Button>
        <Button
          variant="contained"
          onClick={saveAndClose}
          color="primary"
          endIcon={<Save />}
          disabled={!isValid}
        >
          Save
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default SettingsDialog;
