// The Login component is used to allow users to log in to the application.

import React, { FC, useEffect, useMemo } from "react";

import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Slide,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { TransitionProps } from "@mui/material/transitions";

import { useAuthentication } from "../context/authentication";
import {
  validateServerUrl,
  useLogin,
  fetchServerInformation,
} from "../server/authenticating";
import { Notification } from "../common/Notify";
import { useConfiguration } from "../context/configuration";
import { LoginRounded } from "@mui/icons-material";
import { LoadingIndicatorWithBackdrop } from "../common/Loader";

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

// The LoginServerPanel component is used to allow users to select the
// server that they wish to log in to. This decouples the frontend from
// the backend, allowing the frontend to be used with any backend that
// implements the NetGPT API. The user can enter the URL of the server
// that they wish to log in to, and the frontend will attempt to retrieve
// the server's information from the API. If the API returns a valid
// response, the user will be allowed to proceed to the next Login component.
const LoginServerPanel: FC<{
  isOpen: boolean;
}> = ({ isOpen }) => {
  const { authServer, setAuthServer } = useAuthentication();
  const { serverUrl, setServerUrl } = useConfiguration();
  const [serverUrlEntry, setServerUrlEntry] = React.useState<string>(
    serverUrl ?? "",
  );
  const [errorMessage, setErrorMessage] = React.useState<string | null>(null);

  // The handleServerInfo function is used to retrieve the server's authentication information
  const handleServerInfo = async (serverUrl: string) => {
    fetchServerInformation(serverUrl)
      .catch((error) => {
        setErrorMessage(error.message);
        return null;
      })
      .then((serverInfo) => {
        if (serverInfo) {
          setServerUrl(serverUrl);
          setAuthServer(serverInfo);
        }
      });
  };

  const validUrl = useMemo(
    () => validateServerUrl(serverUrlEntry),
    [serverUrlEntry],
  );

  return (
    <Dialog open={isOpen} TransitionComponent={Transition} keepMounted>
      <Notification
        isOpen={errorMessage !== null}
        onClose={() => setErrorMessage(null)}
        severity="error"
        message={errorMessage ?? "An unknown error occurred."}
      />
      <DialogTitle>
        <Stack direction={"column"} spacing={1}>
          <Typography variant={"h6"}>Connect to a NetGPT Server</Typography>
        </Stack>
      </DialogTitle>
      <DialogContent>
        <Stack direction={"column"} spacing={1} padding={1}>
          <TextField
            fullWidth
            id="serverUrl"
            label="Server URL"
            type="text"
            value={serverUrlEntry}
            onChange={(e) => setServerUrlEntry(e.target.value)}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button
          onClick={() => handleServerInfo(serverUrlEntry)}
          endIcon={<LoginRounded />}
          disabled={!validUrl}
        >
          Connect
        </Button>
      </DialogActions>
    </Dialog>
  );
};

// The Login component is used to allow users to log in to the application.
// The user must first select the server that they wish to log in to, and
// then they will be redirected to the server's authentication page.
// During this process, the user will be shown a loading indicator.
const Login: FC<{
  isOpen: boolean;
}> = ({ isOpen }) => {
  const { authServer, setAuthServer, isAuthenticated, setIsAuthenticated } =
    useAuthentication();
  const { serverUrl, setServerUrl } = useConfiguration();
  const [isAuthenticating, setIsAuthenticating] =
    React.useState<boolean>(false);
  const [errorMessage, setErrorMessage] = React.useState<string | null>(null);

  const handleLogin = async () => {
    setIsAuthenticating(true);
    if (authServer) {
      const keycloak = await useLogin(authServer).catch((error) => {
        setErrorMessage(error.message);
        return null;
      });
      if (keycloak?.authenticated) {
        setIsAuthenticated(true);
      }
    }
    setIsAuthenticating(false);
  };

  useEffect(() => {
    if (authServer) {
      handleLogin().then((r) => r);
    }
  }, [authServer]);

  return (
    <>
      <Notification
        isOpen={errorMessage !== null}
        onClose={() => setErrorMessage(null)}
        severity="error"
        message={errorMessage ?? "An unknown error occurred."}
      />
      <LoginServerPanel isOpen={isOpen} />
      {isAuthenticating ? (
        <LoadingIndicatorWithBackdrop label={"Waiting for Authentication..."} />
      ) : null}
    </>
  );
};

export default Login;
