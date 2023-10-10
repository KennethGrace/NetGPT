import React, { FC } from "react";

import "../styles/pulse.css";

interface PulseProps {
  children: React.ReactNode;
  disabled?: boolean;
}

export const Pulse: FC<PulseProps> = ({ children, disabled = false }) => {
  const animatingClass = disabled ? "pulse-disabled" : "pulse-effect";

  return <div className={animatingClass}>{children}</div>;
};

export default Pulse;
