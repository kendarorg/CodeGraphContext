package com.example.api;

import com.example.core.Util;

public class Api {
    public String hello(String name) {
        return Util.greet(name);
    }
}
