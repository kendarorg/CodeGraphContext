package com.example.mybatis;

public interface UserMapper {
    String findNameById(long id);

    void insertUser(String name);
}
