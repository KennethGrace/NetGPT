// The Login component is used to allow users to log in to the application.

import React, { FC, useMemo } from "react";

import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Slide,
  TextField,
} from "@mui/material";

import { TransitionProps } from "@mui/material/transitions";

import { useAuthentication } from "../context/authentication";
import {
  AuthenticationHandler,
  fetchServerInformation,
  validateServerUrl,
} from "../server/authenticating";
import { Notification } from "../common/Notify";
import { useConfiguration } from "../context/configuration";
import { LoginRounded } from "@mui/icons-material";
import {
  saveGreeting,
  saveServerInformation,
  saveServerUrl,
} from "../data/browserCache";
import { getGreeting } from "../server/messaging";

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

// The Login component is used to allow users to select the
// server that they wish to log in to. This decouples the frontend from
// the backend, allowing the frontend to be used with any backend that
// implements the NetGPT API. The user can enter the URL of the server
// that they wish to log in to, and the frontend will attempt to retrieve
// the server's information from the API. If the server's information is
// successfully retrieved, the frontend will attempt to log in to the
// server using the information provided by the server.
const Login: FC<{
  isOpen: boolean;
  onClose: () => void;
}> = ({ isOpen, onClose }) => {
  const { isAuthenticated, setIsAuthenticated, authServer, setAuthServer } =
    useAuthentication();
  const { serverUrl, setServerUrl } = useConfiguration();
  const [serverUrlEntry, setServerUrlEntry] = React.useState<string>(
    serverUrl ?? "",
  );
  const [errorMessage, setErrorMessage] = React.useState<string | null>(null);
  const [loginEnabled, setLoginEnabled] = React.useState<boolean>(false);

  // use Effect to attempt a login if the serverUrl is already set
  React.useEffect(() => {
    if (serverUrl && authServer) {
      handleConnect(serverUrl).then(setIsAuthenticated);
    }
    setLoginEnabled(true);
  }, [serverUrl]);

  // The handleConnect function is used to retrieve the server's authentication information,
  // and then log in to the auth server using the information provided by the api server.
  const handleConnect = async (serverUrl: string) => {
    setServerUrl(serverUrl);
    saveServerUrl(serverUrl);
    const serverInfo = await fetchServerInformation(serverUrl).catch(
      (error) => {
        setErrorMessage("Unable to fetch server information.");
        return null;
      },
    );
    if (!serverInfo) {
      return false;
    }
    await AuthenticationHandler.login(serverInfo);
    saveServerInformation(serverInfo);
    setAuthServer(serverInfo);
    const token = await AuthenticationHandler.getToken();
    if (!token) {
      setErrorMessage("Unable to retrieve token.");
      return false;
    }
    const greeting = await getGreeting(serverUrl, token).catch((error) => {
      setErrorMessage("Unable to retrieve greeting.");
      return null;
    });
    if (greeting) {
      saveGreeting(greeting.sections[0].content);
    } else {
      return false;
    }
    onClose();
    return true;
  };

  const validUrl = useMemo(
    () => validateServerUrl(serverUrlEntry),
    [serverUrlEntry],
  );

  return (
    <Dialog
      open={isOpen}
      keepMounted
      TransitionComponent={Transition}
      onClose={onClose}
      maxWidth={"xs"}
    >
      <Notification
        isOpen={errorMessage !== null}
        onClose={() => setErrorMessage(null)}
        severity="error"
        message={errorMessage ?? "An unknown error occurred."}
      />
      <DialogTitle>Login to a NetGPT Server</DialogTitle>
      <DialogContent>
        <TextField
          autoFocus
          fullWidth
          variant="filled"
          id="serverUrl"
          label="Server URL"
          type="text"
          value={serverUrlEntry}
          onChange={(e) => setServerUrlEntry(e.target.value)}
        />
      </DialogContent>
      <DialogActions>
        <Button
          variant={"contained"}
          onClick={() => handleConnect(serverUrlEntry).then(setIsAuthenticated)}
          endIcon={<LoginRounded />}
          disabled={!validUrl || !loginEnabled}
        >
          Login
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default Login;
