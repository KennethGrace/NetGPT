import { createContext, useContext } from "react";

export type UserLoginInformation = {
  username: string;
  domain: string;
};

export type AuthenticationServerInformation = {
  provider: string;
  server: string;
  realm: string;
  clientId: string;
};

// The interface for the authentication context
export interface Authentication {
  // The authentication status
  isAuthenticated: boolean;
  // The user login information
  setIsAuthenticated: (isAuthenticated: boolean) => void;
  // The server information
  authServer?: AuthenticationServerInformation;
  // Set the server information
  setAuthServer: (authServer: AuthenticationServerInformation) => void;
}

// The authentication context
export const AuthenticationContext = createContext<Authentication>({
  authServer: undefined,
} as Authentication);

// The authentication context provider
export const useAuthentication = () => useContext(AuthenticationContext);
