import React, { FC, useState, useEffect } from "react";

import { Paper, Typography, MenuItem, TextField, Stack } from "@mui/material";

import {
  NetworkSettings,
  useConfiguration,
} from "../../common/configuration";

export interface NetworkFieldProps {
  title: string;
  deviceOptions: string[];
  setSelection: (selection: NetworkSettings) => void;
}

const NetworkField: FC<NetworkFieldProps> = ({
  title,
  deviceOptions,
  setSelection,
}) => {
  const { username, password, deviceType } = useConfiguration().networkSettings ?? {
    username: "",
    password: "",
    deviceType: "",
  };
  const [usernameEntry, setUsernameEntry] = useState<string>(username);
  const [passwordEntry, setPasswordEntry] = useState<string>(password);
  const [deviceEntry, setDeviceEntry] = useState<string>(deviceType);

  // Use Effect to update the Network Selection on state change
  useEffect(() => {
    setSelection({
      username: usernameEntry,
      password: passwordEntry,
      deviceType: deviceEntry,
    });
  }, [usernameEntry, passwordEntry, deviceEntry]);

  return (
    <Paper
      elevation={3}
      sx={{
        padding: 2,
      }}
    >
      <Typography variant="overline">{title}</Typography>
      <Stack direction={"column"} spacing={1}>
        <TextField
          fullWidth
          id="username"
          label="Username"
          type="text"
          variant="filled"
          defaultValue={username}
          onChange={(e) => {
            setUsernameEntry(e.target.value);
          }}
        />
        <TextField
          fullWidth
          id="password"
          label="Password"
          type="password"
          variant="filled"
          defaultValue={password}
          onChange={(e) => {
            setPasswordEntry(e.target.value);
          }}
          helperText="Password for authentication to network devices"
        />
        <TextField
          fullWidth
          select
          id="device-type"
          label="Device Type"
          defaultValue={deviceType}
          variant="filled"
          onChange={(e) => {
            setDeviceEntry(e.target.value);
          }}
        >
          {deviceOptions.map((value, index) => (
            <MenuItem key={index} value={value}>
              {value}
            </MenuItem>
          ))}
        </TextField>
      </Stack>
    </Paper>
  );
};

export default NetworkField;