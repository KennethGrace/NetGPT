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
} from "../../common/configuration";

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
  saveLanguageSettings
} from "../../data/browserCache";

// Lazy Load the LanguageField component
const LanguageField = lazy(() => import("./LanguageField"));
const NetworkField = lazy(() => import("./NetworkField"));

const ServerField: FC<{
  serverEntry: string;
  setEntry: (entry: string) => void;
  updateSettings: () => void;
}> = ({ serverEntry, setEntry, updateSettings }) => {
  const { serverUrl } = useConfiguration();
  const [lastRefresh, setLastRefresh] = useState<string>(serverUrl);

  const isRefreshable: boolean = useMemo(() => {
    return serverEntry.length > 0 && serverEntry !== lastRefresh;
  }, [serverEntry, lastRefresh]);

  const handleRefresh = () => {
    updateSettings();
    setLastRefresh(serverEntry);
  };

  return (
    <Box>
      <Stack direction={"row"} spacing={1} alignItems={"center"}>
        <TextField
          autoFocus
          margin="dense"
          id="serverURL"
          placeholder="http://"
          label="Server URL"
          defaultValue={serverUrl}
          type="text"
          fullWidth
          variant="standard"
          onChange={(e) => {
            setEntry(e.target.value);
          }}
        />
        <Box>
          <IconButton
            onClick={handleRefresh}
            color="primary"
            disabled={!isRefreshable}
          >
            <Refresh />
          </IconButton>
        </Box>
      </Stack>
    </Box>
  );
};

const Transition = React.forwardRef(function Transition(
  props: TransitionProps & {
    children: React.ReactElement<any, any>;
  },
  ref: React.Ref<unknown>
) {
  return <Slide direction="up" ref={ref} {...props} />;
});

export interface SettingsDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

export const SettingsDialog: FC<SettingsDialogProps> = ({
  isOpen,
  onClose,
}) => {
  const { serverUrl, setServerUrl, setNetworkSettings, setLanguageSettings } =
    useConfiguration();
  // State variables for UI
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // State Variables for Form Fields
  const [server, setServer] = useState<string>(serverUrl);
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
    // Check if the server is a valid URL using a regex
    if (!server.match(/^(http):\/\/[^ "]+$/)) {
      return false;
    }
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
    if (Object.entries(language.fields).some(([_, value]) => !(value.length > 0))) {
      console.log("Invalid Language Fields");
      console.log(language.fields)
      return false;
    }
    return true;
  }, [server, network, language]);

  const saveAndClose = () => {
    if (isValid) {
      saveServerUrl(server);
      setServerUrl(server);
      saveLanguageSettings(language);
      setLanguageSettings(language);
      saveNetworkSettings(network);
      setNetworkSettings(network);
      onClose();
    }
  };

  const updateSettings = () => {
    fetchDeviceOptions(server)
      .then((options) => {
        setDeviceTypeOptions(options);
      })
      .catch((error) => {
        setErrorMessage(error.message);
      });
    fetchLanguageSettingsList(server)
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
          <ServerField serverEntry={server} setEntry={setServer} updateSettings={updateSettings} />
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
