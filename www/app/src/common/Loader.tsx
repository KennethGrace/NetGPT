import React, { FC } from "react";

import { Box, CircularProgress, Typography, Backdrop } from "@mui/material";

export interface LoadingIndicatorProps {
  label?: string;
}

/**
 * The LoadingIndicator component.
 *
 * This component displays a circular loading indicator
 * at the center of the viewport.
 *
 * @param {string} label - The label to display below the loading indicator.
 *
 * @example
 * ```tsx
 * <LoadingIndicator label="Loading..."/>
 * ```
 */
export const LoadingIndicator: FC<LoadingIndicatorProps> = ({ label }) => {
  return (
    <Box>
      <Box
        sx={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
        }}
      >
        <CircularProgress
          aria-label="loading icon"
          size={264}
          color="primary"
          thickness={2}
        />
      </Box>
      {label && (
        <Typography
          variant="caption"
          sx={{
            position: "fixed",
            top: "65%",
            left: "50%",
            transform: "translate(-50%, 50%)",
            textAlign: "center",
            fontWeight: "bold",
            fontSize: "1.5rem",
          }}
        >
          {label}
        </Typography>
      )}
    </Box>
  );
};

export const LoadingIndicatorWithBackdrop: FC<LoadingIndicatorProps> = ({
  label,
}) => {
  return (
    <Backdrop open={true} timeout={1000} sx={{
      zIndex: (theme) => theme.zIndex.drawer + 1,
    }}>
      <LoadingIndicator label={label} />
    </Backdrop>
  );
};
